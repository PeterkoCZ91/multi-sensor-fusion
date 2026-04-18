#!/usr/bin/env python3
"""
Fusion Service Installer

Interactive installer that sets up the fusion service without Docker:
- Creates Python venv and installs dependencies
- Wizard for MQTT, Telegram, and sensor configuration
- Scans MQTT for available sensors
- Generates config.yaml
- Installs and starts systemd service
"""

import getpass
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import textwrap
import time

FUSION_DIR = pathlib.Path(__file__).resolve().parent
MIN_PYTHON = (3, 10)

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str):
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str):
    print(f"  {CYAN}→{RESET} {msg}")


def header(msg: str):
    print(f"\n{BOLD}{msg}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val if val else default


def ask_password(prompt: str) -> str:
    return getpass.getpass(f"  {prompt}: ").strip()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def check_python():
    header("1. Python version")
    v = sys.version_info
    if (v.major, v.minor) >= MIN_PYTHON:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    else:
        fail(f"Python {v.major}.{v.minor} < {MIN_PYTHON[0]}.{MIN_PYTHON[1]} required")
        return False


def create_venv():
    header("2. Virtual environment")
    venv_dir = FUSION_DIR / ".venv"
    if venv_dir.exists():
        info(f"Existing venv at {venv_dir}")
        resp = ask("Recreate venv?", "n")
        if resp.lower() != "y":
            ok("Using existing venv")
            return True
        shutil.rmtree(venv_dir)

    info("Creating venv...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"venv creation failed: {result.stderr}")
        return False
    ok(f"Created {venv_dir}")

    info("Installing dependencies...")
    pip = str(venv_dir / "bin" / "pip")
    req = str(FUSION_DIR / "requirements.txt")
    result = subprocess.run(
        [pip, "install", "-r", req],
        capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"pip install failed: {result.stderr[:200]}")
        return False
    ok("Dependencies installed")
    return True


def test_mqtt_connection(host: str, port: int) -> bool:
    """Test basic TCP connectivity to MQTT broker."""
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        return True
    except (socket.error, OSError):
        return False


def test_mqtt_auth(host: str, port: int, user: str, password: str) -> bool:
    """Test MQTT authentication using paho-mqtt from the venv."""
    venv_python = str(FUSION_DIR / ".venv" / "bin" / "python")
    test_code = textwrap.dedent(f"""\
        import sys
        import paho.mqtt.client as mqtt
        connected = False
        def on_connect(c, u, f, rc):
            global connected
            connected = (rc == 0)
            c.disconnect()
        c = mqtt.Client()
        if {repr(user)}:
            c.username_pw_set({repr(user)}, {repr(password)})
        c.on_connect = on_connect
        try:
            c.connect({repr(host)}, {port}, keepalive=5)
            c.loop_start()
            import time; time.sleep(3)
            c.loop_stop()
        except Exception:
            pass
        sys.exit(0 if connected else 1)
    """)
    result = subprocess.run(
        [venv_python, "-c", test_code],
        capture_output=True, text=True, timeout=15)
    return result.returncode == 0


def test_telegram_token(token: str) -> tuple[bool, str]:
    """Test Telegram bot token via getMe API."""
    import urllib.request
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            bot_name = data["result"].get("username", "unknown")
            return True, bot_name
    except Exception:
        pass
    return False, ""


def test_telegram_chat(token: str, chat_id: str) -> bool:
    """Test sending a message to the chat."""
    import urllib.request
    import urllib.parse
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": "✅ Fusion installer: test zpravy OK",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def scan_mqtt_sensors(host: str, port: int, user: str, password: str) -> dict:
    """Scan MQTT for available sensors (ld2412, ld2450, CSI)."""
    venv_python = str(FUSION_DIR / ".venv" / "bin" / "python")
    scan_code = textwrap.dedent(f"""\
        import json, sys, time
        import paho.mqtt.client as mqtt
        found = {{"ld2412": [], "ld2450": [], "csi": []}}
        def on_message(c, u, msg):
            t = msg.topic
            parts = t.split("/")
            if parts[0] == "security" and len(parts) >= 3:
                dev = parts[1]
                if "ld2412" in dev and dev not in found["ld2412"]:
                    found["ld2412"].append(dev)
                elif "ld2450" in dev.lower() or "mw1_" in dev and dev not in found["ld2450"]:
                    found["ld2450"].append(dev)
            elif parts[0] == "esphome" and len(parts) >= 3:
                dev = parts[1]
                if dev.startswith("csi_") and dev not in found["csi"]:
                    found["csi"].append(dev)
        c = mqtt.Client()
        if {repr(user)}:
            c.username_pw_set({repr(user)}, {repr(password)})
        c.on_message = on_message
        c.connect({repr(host)}, {port})
        c.subscribe("security/#")
        c.subscribe("esphome/csi_#")
        c.loop_start()
        time.sleep(5)
        c.loop_stop()
        c.disconnect()
        print(json.dumps(found))
    """)
    try:
        result = subprocess.run(
            [venv_python, "-c", scan_code],
            capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return {"ld2412": [], "ld2450": [], "csi": []}


def configure_mqtt() -> dict:
    header("3. MQTT broker")
    host = ask("MQTT broker IP/host", "mqtt.local")
    port = int(ask("MQTT port", "1883"))

    info(f"Testing connection to {host}:{port}...")
    if test_mqtt_connection(host, port):
        ok("TCP connection OK")
    else:
        fail(f"Cannot connect to {host}:{port}")
        if ask("Continue anyway?", "n").lower() != "y":
            return {}

    header("4. MQTT authentication")
    user = ask("MQTT username (empty = no auth)", "")
    password = ""
    if user:
        password = ask_password("MQTT password")
        info("Testing MQTT auth...")
        if test_mqtt_auth(host, port, user, password):
            ok("MQTT auth OK")
        else:
            fail("MQTT auth failed")
            if ask("Continue anyway?", "n").lower() != "y":
                return {}

    return {"host": host, "port": port, "user": user, "password": password}


def configure_telegram() -> dict:
    header("5. Telegram bot")
    token = ask("Bot token (empty = skip)")
    if not token:
        info("Telegram disabled")
        return {}

    valid, bot_name = test_telegram_token(token)
    if valid:
        ok(f"Bot: @{bot_name}")
    else:
        fail("Invalid token")
        if ask("Continue anyway?", "n").lower() != "y":
            return {}

    header("6. Telegram chat ID")
    chat_id = ask("Chat ID")
    if chat_id and token:
        info("Sending test message...")
        if test_telegram_chat(token, chat_id):
            ok("Test message sent")
        else:
            fail("Could not send test message")

    return {"token": token, "chat_id": chat_id, "cooldown_s": 30}


def scan_sensors(mqtt_cfg: dict) -> dict:
    header("7. Scanning for sensors")
    info("Listening on MQTT for 5 seconds...")
    found = scan_mqtt_sensors(
        mqtt_cfg["host"], mqtt_cfg["port"],
        mqtt_cfg.get("user", ""), mqtt_cfg.get("password", ""))

    for kind, devices in found.items():
        if devices:
            ok(f"{kind}: {', '.join(devices)}")
        else:
            info(f"{kind}: none found")

    return found


def generate_config(mqtt_cfg: dict, tg_cfg: dict, sensors: dict) -> str:
    header("8. Generating config.yaml")

    # Build pairs from discovered sensors
    pairs = []
    ld2412_list = sensors.get("ld2412", [])
    ld2450_list = sensors.get("ld2450", [])
    csi_list = sensors.get("csi", [])

    if ld2412_list and ld2450_list:
        pair_name = ask("Pair name", "living")
        ld2412_id = ask("LD2412 device ID", ld2412_list[0] if ld2412_list else "")
        ld2450_id = ask("LD2450 device ID", ld2450_list[0] if ld2450_list else "")

        pair = {
            "name": pair_name,
            "ld2412_id": ld2412_id,
            "ld2450_id": ld2450_id,
        }

        if csi_list:
            use_csi = ask(f"Use CSI nodes ({', '.join(csi_list)})?", "y")
            if use_csi.lower() == "y":
                pair["csi_ids"] = csi_list
                pair["csi_zones"] = {
                    cid: cid.replace("csi_", "") for cid in csi_list
                }

        pair.update({
            "noise_diff_threshold": 0.3,
            "fusion_mode": "cross_validate",
            "confidence_threshold": 0.7,
            "max_latency_ms": 2000,
            "debounce_in_s": 1.0,
            "clearing_s": 5.0,
        })
        pairs.append(pair)
    else:
        info("No radar pair found, using manual config")
        pair_name = ask("Pair name", "living")
        ld2412_id = ask("LD2412 device ID", f"ld2412_{pair_name}")
        ld2450_id = ask("LD2450 device ID", f"ld2450_{pair_name}")
        pairs.append({
            "name": pair_name,
            "ld2412_id": ld2412_id,
            "ld2450_id": ld2450_id,
            "csi_ids": csi_list or [],
            "noise_diff_threshold": 0.3,
            "fusion_mode": "cross_validate",
            "confidence_threshold": 0.7,
            "max_latency_ms": 2000,
            "debounce_in_s": 1.0,
            "clearing_s": 5.0,
        })

    config = {
        "mqtt": {
            "host": mqtt_cfg["host"],
            "port": mqtt_cfg["port"],
        },
        "pairs": pairs,
        "database": {
            "path": "data/fusion.db",
            "max_size_mb": 500,
        },
        "auto_discovery": {
            "enabled": True,
            "default_pair": pairs[0]["name"] if pairs else "living",
            "csi_prefix": "csi_",
        },
    }

    if mqtt_cfg.get("user"):
        config["mqtt"]["user"] = mqtt_cfg["user"]
        config["mqtt"]["password"] = mqtt_cfg["password"]

    if tg_cfg:
        config["telegram"] = tg_cfg

    import yaml
    config_path = FUSION_DIR / "config.yaml"
    if config_path.exists():
        backup = config_path.with_suffix(".yaml.bak")
        shutil.copy2(config_path, backup)
        info(f"Backed up existing config to {backup.name}")

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    ok(f"Config written to {config_path}")
    return str(config_path)


def create_directories():
    header("9. Creating directories")
    for d in ("data", "calibration"):
        p = FUSION_DIR / d
        p.mkdir(exist_ok=True)
        ok(str(p))


def install_systemd():
    header("10. Systemd service")
    service_src = FUSION_DIR / "fusion-service.service"
    if not service_src.exists():
        fail(f"{service_src} not found")
        return False

    # Read template and fill in paths
    content = service_src.read_text()
    venv_python = FUSION_DIR / ".venv" / "bin" / "python"
    content = content.replace("{fusion_dir}", str(FUSION_DIR))
    content = content.replace("{venv_python}", str(venv_python))
    content = content.replace("%u", os.environ.get("USER", "nobody"))

    # Install to user's systemd dir (--user) or system-wide
    use_system = ask("Install as system service? (requires sudo)", "n")

    if use_system.lower() == "y":
        dest = pathlib.Path("/etc/systemd/system/fusion-service.service")
        info(f"Installing to {dest} (sudo)...")
        tmp = FUSION_DIR / ".fusion-service.service.tmp"
        tmp.write_text(content)
        result = subprocess.run(
            ["sudo", "cp", str(tmp), str(dest)],
            capture_output=True, text=True)
        tmp.unlink(missing_ok=True)
        if result.returncode != 0:
            fail(f"sudo cp failed: {result.stderr}")
            return False
        subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["sudo", "systemctl", "enable", "fusion-service"],
                       capture_output=True)
        subprocess.run(["sudo", "systemctl", "start", "fusion-service"],
                       capture_output=True)
        ok("System service installed and started")
    else:
        user_dir = pathlib.Path.home() / ".config" / "systemd" / "user"
        user_dir.mkdir(parents=True, exist_ok=True)
        dest = user_dir / "fusion-service.service"
        dest.write_text(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "fusion-service"],
                       capture_output=True)
        subprocess.run(["systemctl", "--user", "start", "fusion-service"],
                       capture_output=True)
        ok(f"User service installed at {dest}")

    return True


def verify_service():
    header("11. Verification")
    info("Waiting 5 seconds for service to start...")
    time.sleep(5)

    # Try system service first, then user
    for cmd_prefix in (["sudo", "systemctl"], ["systemctl", "--user"]):
        result = subprocess.run(
            cmd_prefix + ["is-active", "fusion-service"],
            capture_output=True, text=True)
        if result.stdout.strip() == "active":
            ok("fusion-service is active (running)")
            # Show recent logs
            log_cmd = (["sudo", "journalctl", "-u", "fusion-service", "-n", "5", "--no-pager"]
                       if "sudo" in cmd_prefix
                       else ["journalctl", "--user", "-u", "fusion-service", "-n", "5", "--no-pager"])
            log_result = subprocess.run(log_cmd, capture_output=True, text=True)
            if log_result.stdout:
                print(f"\n{CYAN}Recent logs:{RESET}")
                print(log_result.stdout)
            return True

    fail("Service is not running")
    info("Check logs: journalctl -u fusion-service -f")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{BOLD}{'='*50}")
    print(f"  Fusion Service Installer")
    print(f"{'='*50}{RESET}\n")

    if not check_python():
        sys.exit(1)

    if not create_venv():
        sys.exit(1)

    mqtt_cfg = configure_mqtt()
    if not mqtt_cfg:
        sys.exit(1)

    tg_cfg = configure_telegram()
    sensors = scan_sensors(mqtt_cfg)
    generate_config(mqtt_cfg, tg_cfg, sensors)
    create_directories()

    install_svc = ask("Install systemd service?", "y")
    if install_svc.lower() == "y":
        if install_systemd():
            verify_service()

    print(f"\n{GREEN}{BOLD}Fusion service installed!{RESET}")
    print(f"\n  Config:  {FUSION_DIR / 'config.yaml'}")
    print(f"  DB:      {FUSION_DIR / 'data' / 'fusion.db'}")
    print(f"  Logs:    journalctl -u fusion-service -f")
    print(f"  Status:  systemctl status fusion-service")
    print()


if __name__ == "__main__":
    main()
