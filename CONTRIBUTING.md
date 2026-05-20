# Contributing

## Setup

```bash
git clone https://github.com/PeterkoCZ91/multi-sensor-fusion.git
cd multi-sensor-fusion
cp config.yaml.template config.yaml
```

Open `config.yaml` and fill in your values:

- `mqtt.host` — your MQTT broker address
- `mqtt.port` — default `1883`
- Optionally uncomment `mqtt.user` / `mqtt.password` (prefer the `MQTT_PASSWORD` env var)
- Optionally configure `home_assistant.url` and set `HA_TOKEN` via env var
- Configure at least one sensor pair under `pairs`

Sensitive values (`MQTT_PASSWORD`, `HA_TOKEN`, `TELEGRAM_TOKEN`) should go in a `.env` file
or be passed as environment variables — never commit them to `config.yaml`.

## Running with Docker (recommended)

```bash
docker compose up -d
docker compose logs -f fusion
```

To rebuild after code changes:

```bash
docker compose build
docker compose up -d
```

## Running without Docker (development)

```bash
pip install -r requirements.txt
export MQTT_PASSWORD=...
export HA_TOKEN=...
python fusion_service.py
```

## Analytical tools

The `tools/` directory contains standalone Python scripts for offline analysis.
They are not part of the running service — run them separately after collecting data:

```bash
python tools/analyze_weights.py
python tools/optimize_weights.py
python tools/deep_analysis.py
```

## Branch naming

| Prefix | Purpose |
|--------|---------|
| `feature/` | New functionality |
| `fix/` | Bug fixes |
| `docs/` | Documentation only |

Example: `feature/csi-zone-inference`, `fix/mqtt-reconnect-loop`

## Before opening a PR

1. `docker compose build` completes without errors.
2. `docker compose up -d` starts the service and it connects to your MQTT broker
   (check with `docker compose logs -f fusion`).
3. The analytical scripts in `tools/` import and run without crashing.
4. No hardcoded credentials in any file.
5. If new `config.yaml` keys were added, `config.yaml.template` is updated with
   commented-out examples and explanations.
6. `CHANGELOG.md` entry added under an `## [Unreleased]` section.

## What we look for

- No cloud dependencies — everything runs on local infrastructure.
- No hardcoded credentials. Secrets travel through environment variables or `.env`.
- All configuration must be expressible via `config.yaml` or environment variables.
- Backward-compatible changes to `docker-compose.yml` and `config.yaml` structure.
- Clear, self-contained commits with descriptive messages.

## What we will not merge

- Hardcoded IP addresses, tokens, or passwords anywhere in source.
- New mandatory cloud service dependencies.
- Breaking changes to `config.yaml` structure without a migration note.
