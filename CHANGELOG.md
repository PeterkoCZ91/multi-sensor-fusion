# Changelog

All notable changes to the Fusion Multi-Sensor Presence service are documented
in this file. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [v1.0.0] - 2026-04-18

Initial public release.

### Added
- **Fusion engine v2** — weighted-average confidence scoring across LD2412 1D
  radar, LD2450 2D radar, ESPectre WiFi-CSI, and Home Assistant ground-truth
  sensors (PIR, microwave, camera motion/person).
- **Cross-validation bonus** — 2 agreeing sources ×1.15, 3+ sources ×1.25
  (multiplicative).
- **FSM with hysteresis** — `CLEAR → DEBOUNCING → PRESENT → CLEARING → CLEAR`
  with configurable `exit_threshold` below the entry threshold.
- **Anti-pingpong** — per-pair `min_present_s` prevents rapid state oscillation
  (defaults 10–60 s).
- **On-chip composite CSI score** — maps ESPectre's 4-domain weighted signal
  (turbulence, phase, ratio, breathing) onto a 0–1 confidence curve.
- **Adaptive CSI threshold** — per-node auto-calibration from live data with
  persistent `thresholds.json`.
- **Auto-discovery** — subscribes to `esphome/discover/#` for newly-flashed
  CSI nodes; auto-assigns to a configurable default pair.
- **Optional sensors** — any pair can run with a subset of sensors available.
  Sensors marked offline for >60 min are excluded from fusion but reappear
  automatically.
- **Home Assistant REST polling** — per-entity polling of reference sensors
  with `fusion_inputs: true` flag to feed results into fusion scoring.
- **Stuck-sensor detection** — sensors reporting the same state beyond a
  configurable timeout are filtered from fusion.
- **Tapo PTZ controller** — non-blocking camera preset control triggered by
  zone transitions; exponential-backoff retry on failures.
- **Telegram notifications** — start-up banner, alerts, daily summary.
- **MQTT Home Assistant discovery** — pairs automatically publish HA MQTT
  discovery config so a new pair appears as a `binary_sensor` in HA.
- **Offline analysis tools**:
  - `tools/deep_analysis.py` — ground-truth accuracy, per-sensor contribution,
    CSI feature distributions, LD2450 failure-mode analysis, FSM ping-pong
    analysis, temporal patterns.
  - `tools/optimize_weights.py` — data-driven weight tuning via logistic
    regression + grid search on sensor_snapshots.
  - `tools/analyze_weights.py` — legacy weight analysis.
- **SQLite storage** — `events`, `sensor_snapshots`, `ha_ground_truth`,
  `csi_stats_hourly`, `subcarrier_stats_hourly`, `calibration_history`,
  `discovered_nodes`.
- **DB rotation** — automatic pruning of oldest rows above `max_size_mb`
  with VACUUM.
- **Docker and systemd deployment** — `docker-compose.yml` for container
  deployment, `install.py` for interactive bare-metal install with systemd.

### Fixed
- **`cleanup_if_needed` infinite loop** — previously looped forever when
  small tables were empty because `sensor_snapshots` (the actual bulk) was
  not included in the pruning list, and `os.path.getsize` does not shrink
  without VACUUM. Loop now breaks on `rowcount == 0` and prunes every
  rotating table.
- **VACUUM `cannot VACUUM from within a transaction`** — VACUUM now runs in
  autocommit mode (`isolation_level=None`).
