# Security Policy

## Supported Versions

Only the latest release is supported with security fixes.

| Version | Supported |
|---------|-----------|
| latest  | Yes       |
| older   | No        |

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Report issues by email to the maintainer listed in the repository's GitHub profile.
Include a clear description of the vulnerability, steps to reproduce, and potential
impact. You will receive an acknowledgement within **7 days**. If a fix is warranted,
a patch will be prepared before any public disclosure is coordinated.

## Scope

The following areas are in scope:

- **MQTT communication** — unauthenticated or unencrypted message exchange that
  could allow spoofing of sensor data or injection of presence events.
- **Home Assistant API token** — exposure of `HA_TOKEN` through logs, config files
  committed to version control, or process environment leaks.
- **Telegram bot token** — exposure of `TELEGRAM_TOKEN` in the same ways as above.
- **Docker `network_mode: host`** — the service runs with host networking, which
  means it shares the host network stack. Any service binding to a port is directly
  reachable on the host without Docker's usual network isolation. This is intentional
  for MDNS/mDNS discovery but widens the attack surface.

## Hardening Recommendations

### MQTT

- Enable TLS on your MQTT broker (`port: 8883`) and configure the service to verify
  the broker certificate.
- Use a dedicated MQTT user for the fusion service with publish/subscribe limited
  to the topics it needs.
- Pass the password via the `MQTT_PASSWORD` environment variable, not in `config.yaml`.

### Home Assistant

- Create a **long-lived access token** scoped to the minimum required permissions
  (reading the specific `binary_sensor` entities used as ground-truth inputs).
- Rotate the token if it is ever logged or accidentally committed.
- Set the token via the `HA_TOKEN` environment variable only.

### Telegram

- Store `TELEGRAM_TOKEN` in a `.env` file (added to `.gitignore`) and load it via
  Docker Compose's `env_file` directive — do not place it in `docker-compose.yml`
  or `config.yaml`.
- Restrict the bot to your own chat ID in `config.yaml`.

### Docker networking

- Because `network_mode: host` is used, run the container on a dedicated host or
  VLAN that is not directly internet-reachable.
- Use a firewall to restrict inbound connections to only the ports the service
  requires (MQTT client — outbound only; no listening ports required by default).
