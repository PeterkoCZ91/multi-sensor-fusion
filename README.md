# :satellite_antenna: Multi-Sensor Presence Fusion :bulb:

[![CI](https://github.com/PeterkoCZ91/multi-sensor-fusion/actions/workflows/ci.yml/badge.svg)](https://github.com/PeterkoCZ91/multi-sensor-fusion/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Integrated-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![MQTT](https://img.shields.io/badge/MQTT-3.1.1-660066?logo=eclipsemosquitto&logoColor=white)](https://mqtt.org/)

**Room-level presence detection** that weights evidence from 24 GHz radar
(LD2412 / LD2450), WiFi Channel-State-Information (ESPectre), and Home
Assistant reference sensors (PIR, microwave, camera motion/person) into a
single confidence score. Runs as a small Python service. No cloud, no ML
training loop, no sensor is required for the system to work.

> [!TIP]
> **Why fusion?** Any single presence sensor lies some of the time. Radars
> ghost on HVAC drafts, PIRs miss still bodies, CSI drifts on router reboots.
> Combine them, and a weighted majority gets you dramatically better recall
> without wrecking precision — in our deployment: **F1 = 0.95** with all four
> sensor classes cooperating.

---

## Table of Contents

- [In 3 Points](#in-3-points)
- [Why Fusion?](#why-fusion)
- [Who Is This For](#who-is-this-for)
- [What You Need](#what-you-need)
- [Quick Start](#quick-start-10-minutes)
- [How It Works](#how-it-works)
- [Features](#features)
- [System Architecture](#system-architecture)
- [Configuration Reference](#configuration-reference)
- [MQTT Topics](#mqtt-topics)
- [Home Assistant Integration](#home-assistant-integration)
- [Offline Analysis Tools](#offline-analysis-tools)
- [Observed Sensor Behavior](#observed-sensor-behavior)
- [Known Issues & Limitations](#known-issues--limitations)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [FAQ](#faq)
- [Related Projects](#related-projects)
- [Development History](#development-history)
- [Acknowledgments](#acknowledgments)
- [Contributing](#contributing)
- [License](#license)

---

## In 3 Points

1. **It's a fusion service, not a sensor driver.** It does not talk to radar
   hardware directly — each sensor node publishes its own MQTT feed. This
   service listens, scores, and emits one unified presence state per room.
2. **Every sensor is optional.** Any room can run with just a microwave
   sensor from Home Assistant, or just one CSI node, or any combination.
   When a sensor drops offline, it's excluded from the weighted average and
   reappears automatically once it returns.
3. **The data is what makes it work.** The service logs every decision to
   SQLite so you can replay it offline. Two analysis scripts
   (`deep_analysis.py`, `optimize_weights.py`) turn that log into data-driven
   weight tuning — no guesswork about how much each sensor should count.

---

## Why Fusion?

|                              | Radar only (LD2412/LD2450) | WiFi CSI only | Home-Assistant-only (PIR/MW) | **All fused (this project)** |
|------------------------------|:---:|:---:|:---:|:---:|
| **Walking person**           | Excellent | Good | Good | **Excellent (confirmed by multiple physics)** |
| **Stationary person**        | Weak (static energy decays) | Good (breathing) | Good (MW) / Weak (PIR) | **Strong (breathing + MW agree)** |
| **Through thin walls**       | Yes (24 GHz) | Yes (2.4 GHz) | No | Yes |
| **HVAC / draft false trigger** | Radar ghosting common | CSI can see it | PIR fires on air movement | **Lower — cross-validation vetos outliers** |
| **Tamper / spoof resistance**| Single sensor shielded = blind | WiFi jam = blind | Cover MW sensor = blind | **Harder — need to defeat all physics classes** |
| **Cost**                     | $4–8 | $3–5 per node | $15–25 (MW) | Sum of inputs |
| **Failure mode**             | Silent wrong answer | Silent drift | Stuck "on" (HA reports stale) | **Degrades gracefully — bad sensor voted out** |
| **Typical recall**           | 0.70–0.85 | 0.55–0.75 | 0.60–0.90 (MW), 0.30–0.50 (PIR) | **0.95+ after tuning** |
| **Typical precision**        | 0.85–0.95 | 0.80–0.95 | 0.90–0.98 (MW) | **0.95+** |

The weights aren't hand-picked — they fall out of a logistic regression on
the service's own `sensor_snapshots` log. In our deployment, LD2412 came out
as the dominant sensor (weight ~0.50) and PIR came out near zero. Your own
numbers will differ; that's why the tool ships with the service.

---

## Who Is This For

- **Home Assistant users** who need reliable room-level presence for lighting
  / HVAC / alarm automations, and who've noticed single-sensor setups miss or
  false-trigger too often.
- **mmWave radar tinkerers** who already deployed HLK-LD2412 / HLK-LD2450 and
  want to combine them with PIRs and microwave sensors under one logical
  zone.
- **WiFi-CSI enthusiasts** running [ESPectre](https://github.com/PeterkoCZ91/espectre)
  (or the original) who want to prove, with data, that CSI adds value over
  their existing sensors.
- **Security / alarm builders** who need a confidence number, not just a
  boolean, so their logic can distinguish "99% sure someone's there" from
  "one flaky PIR just tripped."

Not for: people who want a plug-and-play binary_sensor without writing a
config file. Expect to spend an hour dialing in weights and thresholds for
each room.

---

## What You Need

### Hardware (any subset)

| Sensor type | Example | Role | ~Cost |
|-------------|---------|------|-------|
| LD2412 mmWave radar | [HLK-LD2412-security](https://github.com/PeterkoCZ91/HLK-LD2412-security) | Dominant — 1D distance + motion/static energy | $4–6 |
| LD2450 mmWave radar | [HLK-LD2450-security](https://github.com/PeterkoCZ91/HLK-LD2450-security) | 2D zone localization, up to 3 targets | $5–8 |
| WiFi CSI node | [ESPectre](https://github.com/PeterkoCZ91/espectre) on ESP32 | Supplementary — stationary breathing, cross-room | $3–5 per node |
| Microwave (Sonoff) | Any Zigbee/Z-Wave MW occupancy sensor | Ground truth + input when not stuck | $15–25 |
| PIR | Generic HA-compatible | Low-weight input, useful mainly for motion onset | $5–15 |
| Camera motion/person | ESP32-CAM + HA, Frigate, etc. | Optional — highest-weight semantic input | varies |

You do not need all of them. The service is built around **graceful
degradation**: offline sensors are skipped, not failed-over.

### Software

- **Python 3.10+** (or Docker, which ships its own)
- **Docker + Docker Compose** (recommended) *or* a Linux host with systemd
- An **MQTT broker** reachable by both sensors and the fusion host
  (Mosquitto works; so does the one bundled with HA)
- Optional: **Home Assistant** 2024+ with a long-lived access token for
  ground-truth sensors and HA discovery

### Required Skills

- Reading and editing a YAML config
- Running a Docker container or a systemd service
- Knowing your sensors' MQTT device IDs (or letting auto-discovery find
  them)

---

## Quick Start (~10 minutes)

### Option A — Docker Compose (recommended)

```bash
# 1. Clone
git clone https://github.com/PeterkoCZ91/multi-sensor-fusion.git
cd multi-sensor-fusion

# 2. Create your config
cp config.yaml.template config.yaml
# edit config.yaml — set `mqtt.host` and at minimum one `pairs:` entry

# 3. (Optional) secrets go in .env, which is gitignored
cp .env.example .env
# edit .env — set MQTT_PASSWORD, TELEGRAM_TOKEN, HA_TOKEN as needed

# 4. Build and run
docker compose up -d --build

# 5. Tail the logs
docker compose logs -f fusion
```

You should see, within a few seconds of the first sensor publishing a
message:

```
[INFO] MQTT connected to mqtt.local:1883
[INFO] Subscribed to N sensor pair(s)
[INFO] FUSION [living] DETECTED conf=0.87 src=ld2450+csi fsm=present  ...
```

### Option B — Interactive install (no Docker)

```bash
python3 install.py
```

`install.py` is a wizard that:

1. Creates a Python virtual environment and installs dependencies.
2. Scans your MQTT broker for LD2412 / LD2450 / CSI nodes and offers to
   auto-configure pairs from them.
3. Prompts for Telegram and Home Assistant integration (both optional).
4. Writes `config.yaml` and optionally installs a systemd unit that runs at
   boot.

---

## How It Works

### The pipeline

```
     Sensor feeds                Fusion engine             Outputs
  +------------------+       +---------------------+    +-----------+
  | LD2412 (MQTT)    |       | 1. per-sensor       |    | MQTT      |
  | LD2450 (MQTT)    |------>|    confidence       |--->| (HA disc) |
  | ESPectre CSI(MQ) |       | 2. weighted average |    | Telegram  |
  | HA REST poll     |       | 3. cross-validation |    | SQLite    |
  +------------------+       | 4. FSM + hysteresis |    | Tapo PTZ  |
                             +---------------------+    +-----------+
                                       |
                                       v
                               sensor_snapshots table
                            (every decision, for replay)
```

### Per-sensor confidence

Each sensor produces a 0..1 confidence score using method appropriate to
its physics:

| Sensor  | Raw signal | Confidence formula |
|---------|------------|--------------------|
| LD2412  | presence flag, distance, energy (motion + static) | base 0.70 + energy z-score bonus, capped 1.00 |
| LD2450  | target count, distance per target | distance-attenuated base + target-count bonus |
| CSI     | on-chip composite score (4-domain weighted: variance, phase turbulence, ratio turbulence, breathing) | nonlinear map → 0..1; fallback to variance ratio |
| MW      | binary | 1.0 when ON and not stuck; 0 otherwise |
| PIR     | binary | 1.0 when ON; small decay window |
| Camera  | binary (motion) + binary (person) | 1.0 per flag, summed with weights |

### Weighted-average fusion

```
           Σ (weight_i · confidence_i)
conf = ────────────────────────────────  ·  cross_validation_bonus
                 Σ weight_i
```

Default weights (data-derived from ~700k snapshots):

| Sensor | Weight |
|--------|-------:|
| LD2412 | 0.50 |
| LD2450 | 0.35 |
| CSI (composite) | 0.25 |
| MW (Sonoff) | 0.40 |
| Camera person | 0.35 |
| Camera motion | 0.20 |
| PIR | 0.10 |

These are defaults. Run `tools/optimize_weights.py` against your own
`fusion.db` to retune for your own deployment.

### Cross-validation bonus

| Agreeing sources | Bonus multiplier |
|------------------|:----------------:|
| 1                | 1.00 |
| 2                | 1.15 |
| 3+               | 1.25 |

### FSM with hysteresis

```
                 conf ≥ confidence_threshold (e.g. 0.55)
   CLEAR ────────────────────────────────────> DEBOUNCING
     ^                                              |
     | clearing_s elapsed                           | debounce_in_s elapsed
     | AND conf < exit_threshold                    v
     |                                          PRESENT ◄──┐
     |                                              |      │ conf recovers
     |                                              |      │ before min_present_s
     +─────────── CLEARING <── conf < exit_threshold ──────┘
                    (hysteresis: exit_threshold ≈ 0.2)
```

Two mechanisms kill oscillation:

- **`exit_threshold`** — you only leave PRESENT when confidence falls below a
  *lower* threshold than the one you needed to enter it. This is textbook
  Schmitt-trigger hysteresis.
- **`min_present_s`** — once in PRESENT, you stay there at least this long,
  even if confidence drops. Tuned to room semantics (30 s for busy rooms,
  60 s for a bedroom where you want zero risk of clearing during sleep).

---

## Features

### :chart_with_upwards_trend: Fusion

| Feature | Description |
|---------|-------------|
| Weighted-average scoring | Each sensor contributes proportional to its trained weight |
| Cross-validation bonus | Multiplicative boost when 2+ sensors agree |
| Optional sensors | Any pair can run with a subset; offline sensors auto-skipped |
| Per-sensor staleness | Each feed has an age threshold; stale data excluded |
| Stuck-sensor filter | Sensors stuck in one state beyond a timeout are ignored |
| Continuity bonus | Persistent presence raises confidence of individual frames |
| Dual-CSI zone inference | Which of two CSI nodes detected motion first → active zone |
| Armed mode | Optional lower thresholds for alarm/security use |

### :satellite: Connectivity

| Feature | Description |
|---------|-------------|
| MQTT auto-discovery | Subscribes to `esphome/discover/#` for new CSI nodes |
| MQTT Home Assistant discovery | Each pair publishes HA-compatible discovery config |
| Home Assistant REST polling | Per-entity polling for ground-truth / fusion input |
| Telegram bot | Startup banner, presence alerts, daily summary |
| Tapo PTZ | Non-blocking camera preset control on zone transitions |
| Graceful degradation | Any sensor/broker/HA can go offline without fusion crashing |

### :floppy_disk: Storage & Analysis

| Feature | Description |
|---------|-------------|
| SQLite logging | Every fusion decision stored for replay |
| `sensor_snapshots` table | Full input + output state per cycle |
| `events` table | State transitions only (smaller, faster queries) |
| Hourly CSI stats aggregation | Background rollup for long-term trends |
| Auto DB rotation | Oldest rows pruned above `max_size_mb`, followed by VACUUM |
| Offline analysis tools | `deep_analysis.py`, `optimize_weights.py` |

### :shield: Reliability

| Feature | Description |
|---------|-------------|
| Watchdog snapshot | Independent timer forces a snapshot even if MQTT is silent |
| MQTT reconnect | Exponential backoff, topic resubscribe on reconnect |
| HA session retry | REST polling continues with per-entity fallback on timeouts |
| CSI calibration persistence | `thresholds.json` survives restarts |
| DB cleanup bounded | Pruning loop breaks on `rowcount=0` (no infinite loop) |

---

## System Architecture

```
+-----------------+          +-----------------+          +-----------------+
| HLK-LD2412      |          | HLK-LD2450      |          | ESPectre CSI    |
| security node   |          | security node   |          | node (ESP32)    |
| (separate repo) |          | (separate repo) |          | (separate repo) |
+--------+--------+          +--------+--------+          +--------+--------+
         |                            |                            |
         | MQTT: security/ld2412_<r>  | MQTT: security/ld2450_<r>  | MQTT: esphome/csi_<r>
         |                            |                            |
         v                            v                            v
     +----------------------------------------------------------------+
     |                      MQTT broker (Mosquitto)                    |
     +---------------------------+------------------------------------+
                                 |
                                 v
                   +--------------------------------+
                   |    Fusion service (this repo)   |
                   |                                 |
                   |  FusionManager per pair:        |
                   |   - collects sensor frames      |
                   |   - computes per-sensor conf    |
                   |   - weighted average + x-check  |
                   |   - FSM + hysteresis            |
                   |   - anti-pingpong guard         |
                   |                                 |
                   |  SensorSnapshotWriter:          |
                   |   - SQLite persistence          |
                   |                                 |
                   |  HA REST poller (optional):     |
                   |   - PIR / MW / camera feeds     |
                   +--------------------------------+
                                 |
          +----------------------+----------------------+
          |                      |                      |
          v                      v                      v
   +-------------+      +---------------+      +------------+
   | MQTT topic  |      | Telegram bot  |      | SQLite DB  |
   | fusion/<r>/ |      | notifications |      | for offline|
   | state       |      |               |      | analysis   |
   | confidence  |      |               |      |            |
   | source      |      |               |      |            |
   +-------------+      +---------------+      +------------+
          |
          v
   Home Assistant
   (auto-discovery)
```

### Repository layout

```
multi-sensor-fusion/
├── fusion_service.py              # Main service (~2900 LOC, single file by design)
│   ├── FusionDatabase             # SQLite layer + rotation / VACUUM
│   ├── FusionManager              # per-pair state, scoring, FSM
│   ├── LD2412Frame / LD2450Frame  # MQTT payload dataclasses
│   ├── CSIFrame                   # ESPectre payload dataclass
│   ├── SensorSnapshotWriter       # per-cycle snapshot persistence
│   ├── HAPoller                   # Home Assistant REST polling
│   ├── TapoCameraController       # Non-blocking Tapo PTZ preset driver
│   ├── TelegramNotifier           # Async Telegram bot client
│   └── AutoDiscoverySubscriber    # ESPHome `discover/#` handler
├── config.yaml.template           # Annotated configuration reference
├── install.py                     # Interactive installer (venv + systemd)
├── docker-compose.yml             # Docker Compose deployment
├── Dockerfile                     # Python 3.12-slim base
├── fusion-service.service         # systemd unit for bare-metal install
├── requirements.txt               # Runtime Python dependencies
├── .env.example                   # Env var template (secrets go here)
├── .gitignore                     # Excludes data/, config.yaml, .env
├── tools/
│   ├── deep_analysis.py           # Comprehensive per-pair analysis + plots
│   ├── optimize_weights.py        # Logistic regression + grid search tuning
│   └── analyze_weights.py         # Legacy analysis (kept for reproducibility)
├── docs/                          # (future) diagrams, notes
├── pictures/                      # (future) screenshots for README
├── CHANGELOG.md                   # Version history (Keep a Changelog format)
├── LICENSE                        # MIT
└── README.md                      # This file
```

Runtime files (gitignored, created on first run):

```
├── config.yaml                    # Your actual config — copy from .template
├── .env                           # Your secrets — copy from .env.example
├── data/
│   ├── fusion.db                  # SQLite store (rotated above max_size_mb)
│   ├── fusion.db-shm              # SQLite WAL shared memory
│   └── fusion.db-wal              # SQLite write-ahead log
└── calibration/
    └── thresholds.json            # Live per-node CSI auto-calibration
```

---

## Configuration Reference

See [`config.yaml.template`](config.yaml.template) for the annotated reference.
Key sections:

### `mqtt`

```yaml
mqtt:
  host: "mqtt.local"      # broker hostname / IP
  port: 1883
  user: "fusion"          # optional; or env MQTT_USER
  password: "..."         # optional; or env MQTT_PASSWORD (preferred)
```

### `pairs[]`

One entry per fusion zone. Minimum viable entry:

```yaml
pairs:
  - name: "living"
    csi_ids: ["csi_living"]
    fusion_mode: "cross_validate"
    confidence_threshold: 0.55
    exit_threshold: 0.20
```

Full option reference:

| Option | Default | Description |
|--------|:-------:|-------------|
| `name` | required | zone label (used in logs, MQTT, HA discovery) |
| `ld2412_id` | *unset* | MQTT client ID of the LD2412 radar node |
| `ld2450_id` | *unset* | MQTT client ID of the LD2450 radar node |
| `csi_ids` | `[]` | List of CSI node IDs (1..N supported) |
| `csi_zones` | auto | Mapping `csi_<id> → zone_label` for multi-CSI setups |
| `fusion_mode` | `cross_validate` | `cross_validate` / `single` / `weighted_avg` |
| `confidence_threshold` | `0.55` | Confidence to enter PRESENT |
| `exit_threshold` | `0.20` | Hysteresis — confidence to leave PRESENT |
| `max_latency_ms` | `2000` | Data older than this is "stale" and excluded |
| `debounce_in_s` | `1.0` | Seconds to confirm presence (DEBOUNCING) |
| `clearing_s` | `20.0` | Seconds in CLEARING before CLEAR |
| `min_present_s` | `30.0` | Anti-pingpong — min time in PRESENT |
| `optional_sensors` | `true` | Tolerate missing sensor feeds |
| `noise_diff_threshold` | `0.3` | Differential threshold for dual-CSI noise detection |
| `score_norm_max` | `0.5` | CSI score divisor for 0..1 normalization |
| `transition_window_s` | `5.0` | Window for A→B zone transition detection |

### `database`

```yaml
database:
  path: "data/fusion.db"
  max_size_mb: 500        # oldest rows pruned above this
```

### `auto_discovery`

Listens on `esphome/discover/#` for newly-flashed CSI nodes.

```yaml
auto_discovery:
  enabled: true
  default_pair: "living"
  csi_prefix: "csi_"
```

### `home_assistant` (optional)

Polls HA REST API for reference sensors. Each sensor can serve as
ground-truth-only (logged for analysis) or as a fusion input
(`fusion_inputs: true`).

See the template for the full `ground_truth_sensors:` structure.

### `tapo_cameras` (optional)

Drives Tapo PTZ presets from zone transitions. Non-blocking, with retries.
See the template for the config shape.

---

## MQTT Topics

### Published by the service

| Topic | Payload | When |
|-------|---------|------|
| `fusion/<pair>/state` | `"on"` / `"off"` | FSM transitions to/from PRESENT |
| `fusion/<pair>/confidence` | float 0..1 | Every fusion cycle |
| `fusion/<pair>/source` | `"ld2450+csi"`, `"ld2412"`, ... | Every cycle |
| `fusion/<pair>/zone` | zone label or `"none"` | Every cycle |
| `fusion/<pair>/fsm` | `clear` / `debouncing` / `present` / `clearing` | On transition |
| `homeassistant/binary_sensor/fusion_<pair>/config` | HA discovery JSON | Once on start |

### Subscribed by the service

| Topic pattern | Source |
|---------------|--------|
| `security/ld2412_<pair>/#` | HLK-LD2412 node |
| `security/ld2450_<pair>/#` | HLK-LD2450 node |
| `esphome/csi_<name>/#` | ESPectre node |
| `esphome/discover/#` | Any ESPHome node announcing itself |

---

## Home Assistant Integration

The service publishes MQTT discovery configs so each fusion pair shows up
as a `binary_sensor.fusion_<pair>` in HA automatically. Attributes include
confidence, source, zone, and FSM state.

To use HA sensors *as* fusion inputs (not just as ground truth for analysis),
set `fusion_inputs: true` on the relevant entries in `ground_truth_sensors`
and generate a long-lived access token in HA (Profile → Security → Long-Lived
Access Tokens), then export it as `HA_TOKEN`.

---

## Offline Analysis Tools

The service logs every decision to `sensor_snapshots`. That table is the
closed-loop you need to tune fusion without guessing.

### `tools/deep_analysis.py`

Comprehensive report against the collected data:

```bash
python3 tools/deep_analysis.py --pair living
```

Outputs (on stdout, plus writes plots to `tools/plots/` if matplotlib is
installed):

1. **Ground truth accuracy** — fusion vs. your best reference sensor (e.g.
   Sonoff microwave). Precision, recall, F1.
2. **Per-sensor contribution** — for each sensor, how often it agreed with
   ground truth, and how often its absence correlated with a wrong fusion
   call.
3. **CSI feature distributions** — composite_score, breathing_score,
   phase_turbulence: detected vs. clear, per node.
4. **LD2450 failure modes** — phantom targets, missed targets, distance
   drift.
5. **Confidence histogram** — how fusion confidence distributes over
   genuine vs. false events.
6. **FSM ping-pong analysis** — how many PRESENT→CLEARING→PRESENT
   oscillations per hour, at which confidence levels.
7. **Hourly / daily patterns** — detection rate by time-of-day.
8. **Per-pair breakdown** — one summary block per zone.

### `tools/optimize_weights.py`

Data-driven weight tuning via logistic regression + grid search:

```bash
python3 tools/optimize_weights.py --pair living
```

Output:

- Fitted logistic-regression weights per sensor
- Grid-search-optimal `confidence_threshold` and `exit_threshold` for best F1
- Suggested `config.yaml` patch
- Observed ceiling F1 (what's achievable with your current sensors)

### `tools/analyze_weights.py`

Legacy analysis. Retained for historical reproducibility; prefer
`optimize_weights.py` for new tuning.

---

## Observed Sensor Behavior

Notes accumulated from extended deployments. Your mileage will vary — these
are symptoms to watch for, not hard rules.

### LD2412 (1D radar)

- **Dominant sensor for most rooms.** Regression consistently gives it the
  highest weight. If you have one sensor, make it this.
- **Energy readings drift** after firmware config writes. Wait 40 s post-boot
  before trusting the first frames.
- **Static energy is the tell** for stationary occupants. Motion alone
  misses breathing bodies. Our scoring uses both.

### LD2450 (2D radar)

- **Phantom targets** in rooms with HVAC vents, ceiling fans, or metal blinds.
  Tune target count and distance gates carefully.
- **Target number fluctuates** rapidly (same person reported as 1 → 2 → 1
  within 200 ms). Average over a short window before using target_count.
- **Distance is trustworthy**; target_id is not (re-used across unrelated
  targets on frame boundaries).

### ESPectre CSI

- **`composite_score`** (the on-chip 4-domain weighted signal) is the
  primary input. `anomaly_ratio` alone showed near-zero discrimination in
  our data and was dropped.
- **`breathing_score`** is a useful supplementary for stationary presence.
  Typical values: ~1–3 clear, ~10–20 occupied, sometimes much higher during
  strong turbulence (HVAC airflow).
- **Calibrates automatically**. A new node typically reaches stable
  thresholds in a few thousand frames.
- **Sensitive to router reboots.** The idle baseline shifts; let the
  auto-calibration re-settle for ~15 minutes before trusting confidence.

### Home Assistant sensors

- **Microwave (Sonoff)** is usually your best ground truth *when powered*.
  But these sensors are known to get stuck at `on` if power is cut — this
  service detects stuck-state sensors and excludes them from fusion until
  they change.
- **PIR weight often comes out near zero** in regression — PIRs fire for
  reasons uncorrelated with occupancy (HVAC airflow through the beam, pets,
  etc.). Keep them as inputs, but don't expect them to lift F1 much.
- **Camera motion** is noisy; **camera person** (when available, e.g.
  Frigate person detection) is the highest-weight HA input.

---

## Known Issues & Limitations

- **Low-recall is the typical failure.** The service is conservative by
  default. In our deployment: precision ~0.96 but recall ~0.42 for the
  busiest room. Retune via `optimize_weights.py` and lower
  `confidence_threshold` if this is your problem too.
- **Zone inference from dual CSI is experimental.** Works when two CSI
  nodes bracket the zone boundary; fails when they're placed asymmetrically.
- **No built-in web UI.** State is exposed via MQTT, logs are stdout, and
  history lives in SQLite. Use HA's UI, or run `deep_analysis.py`, or wire
  a Grafana dashboard against the SQLite DB.
- **Python 3.10+ only.** `match` statements and dict-merge operators are
  used throughout.
- **CSI node firmware mismatch.** Nodes on old firmware (no composite_score
  field) fall back to variance_ratio, which is weaker. Flash all CSI nodes
  to ESPectre 2.3+ for best results.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Service starts but no `FUSION [<pair>] DETECTED ...` in logs | MQTT not connecting | Verify `mqtt.host`/`port`, credentials (`MQTT_USER`/`MQTT_PASSWORD`), firewall. Logs should show `MQTT connected to ...`. |
| Only some pairs emit detections | Subscribed topic name mismatch | Run `mosquitto_sub -h <broker> -t '#' -v`; confirm your `ld2412_<pair>` / `ld2450_<pair>` / `csi_<pair>` IDs match what the firmware publishes. |
| `Offline sensors: <name>: 60m` warning | Feed went silent | Check the sensor node's own logs. Fusion excludes feeds that go quiet; it re-includes them automatically when they return. |
| `DB size … > limit … MB, cleaning up …` then silence | Pre-v1.0.0 cleanup bug | Upgrade to v1.0.0+. Root cause: DELETE loop without `rowcount=0` break + `sensor_snapshots` missing from prune list — see CHANGELOG. |
| Rapid PRESENT↔CLEARING oscillation | `exit_threshold` too close to `confidence_threshold`, or `min_present_s` too low | Widen the hysteresis gap (lower `exit_threshold`) and/or raise `min_present_s`. Run `deep_analysis.py` to identify the flaky sensor. |
| HA entities listed under `ground_truth_sensors` don't affect fusion score | Missing `fusion_inputs: true` flag | Set `fusion_inputs: true` on the entries you want fed into scoring. Without it they're ground-truth only (for analysis). |
| Telegram silent / returns 400 | Missing or wrong `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | Verify both env vars. Test with `curl https://api.telegram.org/bot<TOKEN>/getMe`. `cooldown_s: 0` disables throttling during testing. |
| CSI thresholds keep drifting | Radio-hostile spot (metal, microwave, crowded 2.4 GHz) | Move node, or lock the threshold manually in `thresholds.json` after a known-good calibration and disable auto-recalc for that node. |
| `VACUUM failed: cannot VACUUM from within a transaction` | Pre-v1.0.0 fix missed | Upgrade to v1.0.0+. VACUUM now runs in autocommit (`isolation_level=None`). |
| High CPU with no output | Cleanup loop stuck (pre-v1.0.0) | Upgrade. Diagnostic: `docker stats fusion-service` — 100% CPU with 0 KB net I/O is the signature. |
| ImportError on `install.py` | Wrong Python version | Python 3.10+ required (`match`, dict `|` operators used throughout). |
| Home Assistant binary_sensor never appears | HA MQTT discovery disabled, or different prefix | Confirm HA's MQTT integration uses default `homeassistant/` prefix. Use HA's MQTT integration UI → "Configure" → "Enable discovery". |

### Useful one-liners

```bash
# Watch fusion decisions live
docker logs -f fusion-service | grep FUSION

# Dump the last hour of events from SQLite
sqlite3 -readonly data/fusion.db \
  "SELECT datetime(timestamp,'unixepoch','localtime'), pair_name, event_type, confidence, source
   FROM events
   WHERE timestamp > strftime('%s','now','-1 hour')
   ORDER BY timestamp DESC LIMIT 50;"

# Quick-check which sensors were seen in the last 5 min
sqlite3 -readonly data/fusion.db \
  "SELECT node_id, datetime(MAX(timestamp_hour),'unixepoch','localtime')
   FROM csi_stats_hourly GROUP BY node_id;"

# Force recalibration of a CSI node (restart the service)
docker compose restart fusion
```

---

## Roadmap

- [ ] Per-pair armed/disarmed state exported to MQTT (not just internal)
- [ ] Live Grafana dashboard template against the SQLite DB
- [ ] Native HA custom component (avoid MQTT round-trip for HA-only users)
- [ ] Auto-weight retuning as a periodic background task
- [ ] Zigbee2MQTT sensors as first-class inputs (currently go via HA REST)

---

## FAQ

**Q: Can I use this with ESPHome's own presence_sensor?**
A: Yes — its state is exposed over MQTT; add it to `ground_truth_sensors`
with `fusion_inputs: true`.

**Q: Does it need the internet?**
A: No. All traffic is local MQTT + local HA. Telegram needs internet only
if enabled.

**Q: How much CPU / RAM?**
A: Idle under 1% of one core on a Raspberry Pi 4. Steady-state ~30 MB RSS
per 1000 snapshots/day. SQLite DB grows ~5 MB per 10 000 decisions;
`max_size_mb` caps it.

**Q: Can I run multiple fusion instances?**
A: Yes, as long as their MQTT topic prefixes don't collide. We
deliberately hard-code `fusion/` as the output prefix today; if you need
two instances, patch it.

**Q: Why weighted-average and not a neural net?**
A: Because the first few hundred snapshots of a new deployment are enough
to train this. A learned model would need you to run it for a month
before it mattered, and it wouldn't be any more explainable when something
went wrong.

---

## Related Projects

Fusion consumes feeds from these sensor-node firmware projects. All are MIT
or GPL-3.0 licensed and self-contained — you only need the ones matching
the hardware you actually own.

| Repo | Hardware | Network | License | Role in fusion |
|------|----------|---------|:-------:|----------------|
| [HLK-LD2412-security](https://github.com/PeterkoCZ91/HLK-LD2412-security) | ESP32 + HLK-LD2412 | WiFi | MIT | Dominant sensor (1D radar) |
| [HLK-LD2412-POE-security](https://github.com/PeterkoCZ91/HLK-LD2412-POE-security) | ESP32 + LAN8720A + LD2412 | PoE/Ethernet | MIT | Dominant sensor (wired) |
| [HLK-LD2450-security](https://github.com/PeterkoCZ91/HLK-LD2450-security) | ESP32 + HLK-LD2450 | WiFi | MIT | 2D radar, zone localization |
| [HLK-LD2412-POE-WiFi-CSI-security](https://github.com/PeterkoCZ91/HLK-LD2412-POE-WiFi-CSI-security) | ESP32-PoE + LD2412 + CSI | PoE + WiFi (CSI-only) | GPL-3.0 | Dual-sensor node (both radar and CSI in one board) |
| [espectre](https://github.com/PeterkoCZ91/espectre) | ESP32 (any) | WiFi | GPL-3.0 | Standalone CSI node |

Any combination of these (or none — just HA sensors) produces a valid
fusion deployment. Start with whichever hardware you already have.

---

## Development History

Released milestones (detail in [CHANGELOG.md](CHANGELOG.md)):

| Version | Highlights |
|---------|-----------|
| v1.0.0 | Initial public release: weighted-average fusion, FSM with hysteresis, anti-pingpong, cross-validation bonus, optional sensors, auto-discovery, HA REST polling, MQTT HA discovery, Tapo PTZ, Telegram notifications, SQLite logging with safe DB rotation, offline analysis tools |

Pre-release internal development (no tagged builds):

| Phase | Highlights |
|-------|-----------|
| Phase 1 | Initial 3-sensor fusion (LD2412 + LD2450 + single CSI) with additive scoring |
| Phase 2 | HA integration: REST polling, stuck-sensor filter, HA discovery output |
| Phase 3 | CSI rewrite: dropped `anomaly_ratio`, adopted on-chip `composite_score`, added `breathing_score`, dual-CSI zone inference |
| Phase 4 | Weighted-average rewrite: data-derived weights from logistic regression, cross-validation bonus, lowered thresholds, exit-threshold hysteresis |
| Phase 5 | Anti-pingpong: `min_present_s`, continuity bonus, FSM state machine formalized |
| Phase 6 | Public release hardening: DB cleanup fix, VACUUM autocommit, full docs, scrubbed secrets |

---

## Acknowledgments

- **[Francesco Pace](https://github.com/francescopace)** —
  [ESPectre](https://github.com/francescopace/espectre) WiFi CSI motion
  detection (GPLv3). The CSI side of this fusion is built on ESPectre's
  on-chip composite score; the service just consumes its MQTT output.
- **[Home Assistant](https://www.home-assistant.io/)** — reference sensors,
  MQTT discovery protocol, long-lived access token support, and the
  ecosystem that makes this service useful.
- **[Hi-Link](https://www.hlktech.net/)** — HLK-LD2412 and HLK-LD2450
  mmWave radar modules, and the community-reverse-engineered UART
  protocol.
- **[Eclipse Paho / Mosquitto](https://mosquitto.org/)** — the MQTT broker
  and client library this service depends on.
- **[Tasmota / Staars](https://github.com/Staars)** — community LD2412
  protocol research that the upstream firmware projects build on.
- **[SA-WiSense](https://github.com/)** — inspiration for the ratio-
  turbulence CSI feature.

---

## Contributing

Contributions are welcome — especially:

- **Weight-tuning reports** for different hardware mixes (we genuinely
  want to know what the regression spits out on non-Sonoff MW sensors, on
  Frigate+camera setups, on Zigbee PIR, etc.).
- **Firmware-quirk notes** for LD2412 / LD2450 versions we haven't seen.
- **New fusion inputs** — any binary sensor behind HA can already be
  wired in via `ground_truth_sensors` + `fusion_inputs: true`, but native
  support for Zigbee2MQTT / Z-Wave JS is on the roadmap and welcome.
- **Bug reports** with: your `config.yaml` (secrets redacted!), a few
  lines of log output, and, if possible, a dump of the offending rows
  from `sensor_snapshots` (SQLite `.dump` is fine).

### Workflow

1. Fork the repository on GitHub.
2. Create a feature branch (`git checkout -b feat/my-feature`).
3. Make your changes and add a line to `CHANGELOG.md` under an
   `[Unreleased]` heading.
4. Run a smoke test (`python3 -c "import fusion_service"` must succeed,
   and the service should start cleanly against a real broker).
5. Commit with a clear imperative message (`feat: ...`, `fix: ...`,
   `docs: ...`, `refactor: ...`).
6. Push to the branch and open a Pull Request against `master`.

### Coding conventions

- Python 3.10+. Use `match` statements and dict-merge operators where
  they improve readability.
- Type hints on public methods and dataclasses. Keep internal helpers
  pragmatic — not every `_tmp` variable needs one.
- Single-file `fusion_service.py` is deliberate. Resist the urge to split
  until the file crosses ~3500 LOC *and* a clear logical boundary
  emerges. New classes go at the bottom of their topical section.
- Keep `__init__` methods short. Long setup goes in `_configure_*`
  helpers.
- Log sparingly at INFO (one line per fusion cycle max), loudly at
  WARNING, and noisily at DEBUG.

### Issue reporting

Please include:

- Service version (first line of `docker logs fusion-service` after
  start-up).
- A full fusion cycle log line showing `FUSION [<pair>] ... scores=[...]`.
- Your `config.yaml` with secrets replaced by `REDACTED`.
- If the bug involves DB state: `sqlite3 data/fusion.db .schema` and the
  row counts per table.

---

## License

MIT — see [LICENSE](LICENSE).

This service is under the permissive MIT license. The upstream
firmware projects it consumes have their own licenses (MIT or GPL-3.0 per
the table in [Related Projects](#related-projects)); using them as
*data sources* over MQTT does not create a derivative work, so the fusion
service remains MIT-licensable even when paired with GPL-3.0 nodes.

If you fork and bundle any GPL-3.0 source *into* this project, however,
re-license accordingly.
