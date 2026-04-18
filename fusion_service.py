#!/usr/bin/env python3
"""
Sensor Fusion Service v2: LD2412 + LD2450 + WiFi CSI

Three-layer presence detection with:
- Z-score statistical analysis (adaptive thresholds)
- 4-state machine with hysteresis (CLEAR → DEBOUNCING → PRESENT → CLEARING)
- Cross-validation across mmWave radars + WiFi CSI

Runs as a standalone Python service (RPi / NAS / Docker).
"""

import collections
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import urllib.request
import urllib.parse

import threading

import paho.mqtt.client as mqtt
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fusion")


# ---------------------------------------------------------------------------
# Enums & Data classes
# ---------------------------------------------------------------------------

class FusionState(Enum):
    CLEAR = "clear"
    DEBOUNCING = "debouncing"     # Motion detected, waiting to confirm
    PRESENT = "present"
    CLEARING = "clearing"         # Motion lost, waiting to confirm absence


@dataclass
class TargetData:
    x: int = 0   # mm
    y: int = 0   # mm
    speed: int = 0  # mm/s (from LD2450 firmware)
    last_update: float = 0.0  # timestamp of last coordinate update

    @property
    def distance_mm(self) -> float:
        return math.sqrt(self.x ** 2 + self.y ** 2)


@dataclass
class LD2412State:
    device_id: str = ""
    last_update: float = 0.0
    presence: bool = False
    distance_cm: int = 0
    moving_energy: int = 0
    static_energy: int = 0
    direction: str = "static"
    alarm_state: str = "disarmed"


@dataclass
class LD2450State:
    device_id: str = ""
    last_update: float = 0.0
    presence: bool = False
    target_count: int = 0
    targets: list = field(default_factory=lambda: [TargetData(), TargetData(), TargetData()])
    alarm_state: str = "disarmed"


@dataclass
class CSIState:
    device_id: str = ""
    last_update: float = 0.0
    motion: bool = False
    movement_score: float = 0.0   # Moving variance from ESPectre (get_motion_metric)
    turbulence: float = 0.0       # Raw σ from get_last_turbulence()
    variance: float = 0.0         # Moving variance (get_motion_metric, same as movement_score)
    mean_turbulence: float = 0.0  # Mean over buffer window (baseline)
    amplitude_sum: float = 0.0    # Sum of 12 subcarrier amplitudes
    active_threshold: float = 0.0 # Current detection threshold
    packets_total: int = 0        # Total packets processed
    calibrating: bool = False     # True during calibration
    detector_ready: bool = False  # True when buffer is filled
    subcarrier_amplitudes: list = field(default_factory=list)  # 12 per-subcarrier values
    phase_turbulence: float = 0.0  # std inter-subcarrier phase differences (v2.4.0-fork)
    idle_mean_turbulence: float = 0.0  # EMA baseline updated only during IDLE
    ratio_turbulence: float = 0.0      # SA-WiSense: std of adjacent subcarrier amplitude ratios
    composite_score: float = 0.0       # On-chip 4-domain weighted: turb/phase/ratio/breathing deviations from idle baselines
    amplitude_deviation: float = 0.0   # amp_sum / idle_baseline, <0.85 = presence
    breathing_score: float = 0.0       # RMS of bandpass-filtered amplitude (0.08-0.6 Hz)


@dataclass
class HASensorInput:
    """HA sensor that actively contributes to fusion confidence."""
    entity_id: str = ""
    sensor_type: str = ""     # mw_occupancy, pir_motion, camera_motion, camera_person
    weight: float = 0.0       # confidence contribution when ON
    state: bool = False       # current on/off
    last_update: float = 0.0  # timestamp
    state_since: float = 0.0  # when state last changed (for stuck detection)
    event_window_s: float = 120.0  # for event. entities: how long after event to stay "on"
    stuck_timeout_s: float = 600.0  # filter sensor stuck ON longer than this (per-sensor override)
    warmup_s: float = 0.0     # reduce weight by 50% for first N seconds after turning ON


@dataclass
class DualCSIResult:
    active_zone: str = "none"           # "obyvak" / "chodba" / "transition" / "none"
    movement_direction: str = "none"    # "A→B" / "B→A" / "none"
    differential_score: float = 0.0     # |score_a - score_b|
    is_noise: bool = False              # True if both high + low differential
    confidence_adjust: float = 0.0      # Penalty/bonus to apply


@dataclass
class FusionResult:
    presence: bool = False
    confidence: float = 0.0
    source: str = "none"
    distance_mm: float = 0.0
    alarm_state: str = "disarmed"
    state: FusionState = FusionState.CLEAR
    reason: str = ""
    active_zone: str = "none"
    movement_direction: str = "none"


# ---------------------------------------------------------------------------
# Z-Score Analyzer — adaptive statistical detection
# ---------------------------------------------------------------------------

class ZScoreAnalyzer:
    """Running z-score analysis with incremental Welford's statistics."""

    def __init__(self, window_size: int = 60, z_threshold: float = 2.5):
        self.window_size = window_size
        self.z_threshold = z_threshold
        self._buffer: collections.deque = collections.deque(maxlen=window_size)
        self._calibrated = False
        self._running_mean: float = 0.0
        self._running_m2: float = 0.0  # sum of squared deviations

    def update(self, value: float) -> float:
        """Add a value and return its z-score. Returns 0.0 if not yet calibrated."""
        n = len(self._buffer)

        if n < self.window_size:
            # Buffer filling: standard Welford's accumulation
            self._buffer.append(value)
            n += 1
            delta = value - self._running_mean
            self._running_mean += delta / n
            delta2 = value - self._running_mean
            self._running_m2 += delta * delta2
        else:
            # Buffer full: sliding window Welford's (replace oldest)
            old_val = self._buffer[0]
            self._buffer.append(value)
            new_mean = self._running_mean + (value - old_val) / self.window_size
            self._running_m2 += ((value - old_val) *
                                 (value - new_mean + old_val - self._running_mean))
            if self._running_m2 < 0.0:
                self._running_m2 = 0.0  # clamp float drift
            self._running_mean = new_mean

        if len(self._buffer) < 10:
            return 0.0
        self._calibrated = True

        variance = self._running_m2 / len(self._buffer)
        std = math.sqrt(variance) if variance > 0 else 0.001

        return (value - self._running_mean) / std

    def is_anomalous(self, value: float) -> bool:
        """Returns True if value deviates significantly from baseline."""
        z = self.update(value)
        return abs(z) > self.z_threshold

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    @property
    def baseline_mean(self) -> float:
        return self._running_mean


# ---------------------------------------------------------------------------
# Dual-CSI Differential Analyzer
# ---------------------------------------------------------------------------

class DualCSIAnalyzer:
    """Analyzes two CSI nodes for zone detection and movement direction."""

    def __init__(self, zone_map: dict[str, str], noise_diff_threshold: float = 0.3,
                 score_norm_max: float = 0.5, transition_window: float = 5.0):
        self.zone_map = zone_map          # {"csi_obyvak": "obyvak", "csi_chodba": "chodba"}
        self.noise_diff_threshold = noise_diff_threshold
        self.score_norm_max = score_norm_max  # score divisor for 0-1 normalization
        self._first_detect_id: Optional[str] = None
        self._first_detect_time: float = 0.0
        self._transition_window: float = transition_window
        self._ids = list(zone_map.keys())

    def analyze(self, csi_states: dict[str, CSIState], now: float,
                max_stale_s: float = 2.0) -> DualCSIResult:
        """Analyze dual CSI states for zone/direction/noise."""
        result = DualCSIResult()

        if len(self._ids) < 2:
            # Single CSI — just report its zone if active
            for csi_id in self._ids:
                st = csi_states.get(csi_id)
                if st and st.motion and (now - st.last_update) < max_stale_s:
                    result.active_zone = self.zone_map[csi_id]
            return result

        id_a, id_b = self._ids[0], self._ids[1]
        st_a = csi_states.get(id_a)
        st_b = csi_states.get(id_b)

        a_fresh = st_a and (now - st_a.last_update) < max_stale_s
        b_fresh = st_b and (now - st_b.last_update) < max_stale_s

        a_active = a_fresh and st_a.motion
        b_active = b_fresh and st_b.motion

        score_a = st_a.movement_score if a_fresh and st_a else 0.0
        score_b = st_b.movement_score if b_fresh and st_b else 0.0

        # Normalize scores to 0-1 range
        norm_a = min(score_a / self.score_norm_max, 1.0)
        norm_b = min(score_b / self.score_norm_max, 1.0)
        result.differential_score = abs(norm_a - norm_b)

        # --- Zone logic ---
        if a_active and b_active:
            if result.differential_score < self.noise_diff_threshold:
                # Both active with similar scores → likely noise
                result.is_noise = True
                result.active_zone = "none"
                result.confidence_adjust = -0.15
            else:
                # Both active with different scores → transition
                result.active_zone = "transition"
                result.confidence_adjust = 0.2
        elif a_active and not b_active:
            result.active_zone = self.zone_map[id_a]
            result.confidence_adjust = 0.1
        elif b_active and not a_active:
            result.active_zone = self.zone_map[id_b]
            result.confidence_adjust = 0.1
        else:
            result.active_zone = "none"

        # --- Sequential transition detection ---
        if a_active or b_active:
            current_id = id_a if (a_active and (not b_active or norm_a > norm_b)) else id_b

            if self._first_detect_id is None:
                self._first_detect_id = current_id
                self._first_detect_time = now
            elif (current_id != self._first_detect_id and
                  (now - self._first_detect_time) <= self._transition_window):
                zone_from = self.zone_map[self._first_detect_id]
                zone_to = self.zone_map[current_id]
                result.movement_direction = f"{zone_from}→{zone_to}"
                result.confidence_adjust = 0.2
                # Reset for next transition
                self._first_detect_id = current_id
                self._first_detect_time = now
            elif (now - self._first_detect_time) > self._transition_window:
                # Window expired, reset
                self._first_detect_id = current_id
                self._first_detect_time = now
        else:
            self._first_detect_id = None

        return result


# ---------------------------------------------------------------------------
# CSI Auto-Calibration (self-calibrating percentile approach)
# ---------------------------------------------------------------------------

class CSICalibrationModel:
    """Per-node CSI threshold from variance distribution percentiles.

    Collects all variance samples and finds the natural idle/motion
    boundary using P80 (noise ceiling) and P95 (motion floor).
    No external ground truth needed — works even when mmWave is
    unreliable (ghost targets, static presence through walls).
    """

    def __init__(self, node_id: str, min_samples: int = 300):
        self.node_id = node_id
        self.variances: collections.deque = collections.deque(maxlen=5000)
        self.idle_variances: collections.deque = collections.deque(maxlen=5000)
        self.current_threshold: float = 0.025  # initial fallback
        self.last_recalc: float = 0.0
        self.min_samples = min_samples
        self.idle_n: int = 0    # samples below threshold (informational)
        self.motion_n: int = 0  # samples above threshold (informational)

    def record(self, variance: float, is_idle: bool = True):
        if variance <= 0:
            return
        self.variances.append(variance)
        if is_idle:
            self.idle_variances.append(variance)
        if variance < self.current_threshold:
            self.idle_n += 1
        else:
            self.motion_n += 1

    def recalculate(self) -> Optional[float]:
        # Use only idle samples for threshold calculation to avoid
        # absorbing motion variance into the idle baseline (P6)
        samples = self.idle_variances if len(self.idle_variances) >= self.min_samples else self.variances
        if len(samples) < self.min_samples:
            return None
        sorted_v = sorted(samples)
        n = len(sorted_v)
        p80 = sorted_v[int(n * 0.80)]
        # Use P90 instead of P95 to reduce outlier sensitivity
        p90 = sorted_v[int(n * 0.90)]
        # Need clear separation: motion samples must push p90 above noise
        if p80 <= 0 or p90 / p80 < 1.5:
            return None  # No bimodal separation yet, keep fallback
        # Threshold = 30% from noise ceiling toward motion floor
        new_thr = p80 + (p90 - p80) * 0.3
        new_thr = max(new_thr, 0.005)
        new_thr = min(new_thr, 0.08)  # cap: never above 0.08
        self.current_threshold = new_thr
        self.last_recalc = time.time()
        return new_thr

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "threshold": self.current_threshold,
            "idle_n": self.idle_n,
            "motion_n": self.motion_n,
            "last_recalc": self.last_recalc,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CSICalibrationModel":
        model = cls(node_id=data["node_id"])
        model.current_threshold = data.get("threshold", 0.025)
        model.idle_n = data.get("idle_n", 0)
        model.motion_n = data.get("motion_n", 0)
        model.last_recalc = data.get("last_recalc", 0.0)
        return model


# ---------------------------------------------------------------------------
# Amplitude-Based Static Presence Tracker
# ---------------------------------------------------------------------------

class AmplitudeBaselineTracker:
    """Track CSI amplitude baseline for static presence detection.

    When someone stands still between AP and sensor, signal is attenuated
    (amplitude drops below baseline) while variance stays low.
    """

    def __init__(self, window: int = 300, recalc_interval: float = 10.0):
        self._buffer: collections.deque = collections.deque(maxlen=window)
        self._baseline_p20: float = 0.0
        self._last_recalc: float = 0.0
        self._recalc_interval = recalc_interval

    def update(self, amplitude: float, variance: float, threshold: float,
               now: float = 0.0) -> bool:
        """Feed amplitude sample and return True if signal is blocked (static presence)."""
        if amplitude <= 0:
            return False
        self._buffer.append(amplitude)

        if (now - self._last_recalc) >= self._recalc_interval and len(self._buffer) >= 30:
            sorted_a = sorted(self._buffer)
            self._baseline_p20 = sorted_a[int(len(sorted_a) * 0.20)]
            self._last_recalc = now

        if self._baseline_p20 <= 0:
            return False

        blocked = (amplitude < self._baseline_p20 * 0.85 and
                   variance < threshold * 0.5)
        return blocked

    @property
    def baseline_p20(self) -> float:
        return self._baseline_p20


# ---------------------------------------------------------------------------
# Per-Subcarrier Analyzer
# ---------------------------------------------------------------------------

class SubcarrierAnalyzer:
    """Per-subcarrier rolling z-score anomaly detector.

    Tracks 12 subcarrier amplitudes independently.  Counts how many
    subcarriers show z-score anomalies each update cycle — this provides
    a richer signal than the scalar amplitude sum alone.

    Uses incremental Welford's statistics for O(1) per-subcarrier updates.
    """

    NUM_SC = 12

    def __init__(self, window: int = 60, z_threshold: float = 2.5):
        self._window = window
        self._buffers: list[collections.deque] = [
            collections.deque(maxlen=window) for _ in range(self.NUM_SC)
        ]
        self._z_threshold = z_threshold
        # Incremental stats per subcarrier (Welford's)
        self._means: list[float] = [0.0] * self.NUM_SC
        self._m2s: list[float] = [0.0] * self.NUM_SC
        # Baseline tracking for recalibration trigger
        self._baseline_samples: int = 0
        self._baseline_sums: list[float] = [0.0] * self.NUM_SC
        self._baseline_means: list[float] = [0.0] * self.NUM_SC
        self._baseline_locked = False
        self._shift_counter: int = 0
        self._last_recalib: float = 0.0

    def update(self, amplitudes: list[float]) -> int:
        """Feed 12 amplitudes, return count of anomalous subcarriers."""
        anomaly_count = 0
        for i in range(min(len(amplitudes), self.NUM_SC)):
            val = amplitudes[i]
            buf = self._buffers[i]
            n = len(buf)

            if n < self._window:
                # Filling phase
                buf.append(val)
                n += 1
                delta = val - self._means[i]
                self._means[i] += delta / n
                delta2 = val - self._means[i]
                self._m2s[i] += delta * delta2
            else:
                # Sliding window
                old_val = buf[0]
                buf.append(val)
                new_mean = self._means[i] + (val - old_val) / self._window
                self._m2s[i] += ((val - old_val) *
                                 (val - new_mean + old_val - self._means[i]))
                if self._m2s[i] < 0.0:
                    self._m2s[i] = 0.0
                self._means[i] = new_mean

            if len(buf) < 10:
                continue
            variance = self._m2s[i] / len(buf)
            std = math.sqrt(variance) if variance > 0 else 0.001
            z = abs((val - self._means[i]) / std)
            if z > self._z_threshold:
                anomaly_count += 1

            # Baseline accumulation (first 300 samples ~ 10 min at 2s interval)
            if not self._baseline_locked:
                self._baseline_sums[i] += val
        if not self._baseline_locked:
            self._baseline_samples += 1
            if self._baseline_samples >= 300:
                for i in range(self.NUM_SC):
                    self._baseline_means[i] = self._baseline_sums[i] / self._baseline_samples
                self._baseline_locked = True
        return anomaly_count

    def check_shift(self, amplitudes: list[float], now: float,
                    cooldown: float = 600.0) -> bool:
        """Check if subcarrier baseline has shifted significantly.

        Returns True if recalibration should be triggered.
        """
        if not self._baseline_locked:
            return False
        if (now - self._last_recalib) < cooldown:
            return False

        shifted = 0
        for i in range(min(len(amplitudes), self.NUM_SC)):
            bl = self._baseline_means[i]
            if bl <= 0:
                continue
            if abs(amplitudes[i] - bl) / bl > 0.20:
                shifted += 1

        if shifted >= 6:  # at least half subcarriers shifted
            self._shift_counter += 1
        else:
            self._shift_counter = 0

        if self._shift_counter >= 30:  # 30 consecutive shifted readings
            self._shift_counter = 0
            self._last_recalib = now
            # Reset baseline for next cycle
            self._baseline_locked = False
            self._baseline_samples = 0
            self._baseline_sums = [0.0] * self.NUM_SC
            return True
        return False


# ---------------------------------------------------------------------------
# SQLite Database for event logging and statistics
# ---------------------------------------------------------------------------

class FusionDatabase:
    """SQLite storage for fusion events, calibration history, CSI stats, and node discovery."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        pair_name TEXT NOT NULL,
        event_type TEXT NOT NULL,
        state TEXT,
        confidence REAL,
        source TEXT,
        zone TEXT,
        distance_mm REAL,
        fsm_state TEXT,
        details_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
    CREATE INDEX IF NOT EXISTS idx_events_pair ON events(pair_name);

    CREATE TABLE IF NOT EXISTS calibration_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        node_id TEXT NOT NULL,
        threshold REAL,
        idle_n INTEGER,
        motion_n INTEGER,
        p80 REAL,
        p90 REAL
    );
    CREATE INDEX IF NOT EXISTS idx_cal_node ON calibration_history(node_id);

    CREATE TABLE IF NOT EXISTS csi_stats_hourly (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_hour REAL NOT NULL,
        node_id TEXT NOT NULL,
        avg_variance REAL,
        avg_turbulence REAL,
        avg_amplitude REAL,
        motion_pct REAL,
        packet_count INTEGER,
        avg_phase_turbulence REAL,
        avg_ratio_turbulence REAL,
        avg_breathing_score REAL
    );
    CREATE INDEX IF NOT EXISTS idx_csi_stats_ts ON csi_stats_hourly(timestamp_hour);

    CREATE TABLE IF NOT EXISTS subcarrier_stats_hourly (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,
        node_id TEXT NOT NULL,
        subcarrier INTEGER NOT NULL,
        mean REAL,
        variance REAL,
        sample_count INTEGER,
        UNIQUE(timestamp, node_id, subcarrier)
    );
    CREATE INDEX IF NOT EXISTS idx_sc_stats_ts ON subcarrier_stats_hourly(timestamp);

    CREATE TABLE IF NOT EXISTS discovered_nodes (
        node_id TEXT PRIMARY KEY,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        ip TEXT,
        zone TEXT,
        auto_added INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS ha_ground_truth (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        room TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        sensor_type TEXT NOT NULL,
        state TEXT NOT NULL,
        csi_node TEXT,
        csi_variance REAL,
        csi_motion INTEGER,
        csi_turbulence REAL,
        fusion_state TEXT,
        fusion_confidence REAL
    );
    CREATE INDEX IF NOT EXISTS idx_gt_ts ON ha_ground_truth(timestamp);
    CREATE INDEX IF NOT EXISTS idx_gt_room ON ha_ground_truth(room);

    CREATE TABLE IF NOT EXISTS sensor_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        pair_name TEXT NOT NULL,
        fusion_state TEXT,
        fusion_confidence REAL,
        fusion_source TEXT,
        fsm_state TEXT,
        ld2450_target_count INTEGER,
        ld2450_distance_mm REAL,
        ld2450_targets_json TEXT,
        ld2412_presence INTEGER,
        ld2412_distance_cm REAL,
        ld2412_direction TEXT,
        csi_json TEXT,
        ha_sensors_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_snap_ts ON sensor_snapshots(timestamp);
    CREATE INDEX IF NOT EXISTS idx_snap_pair ON sensor_snapshots(pair_name);
    """

    def __init__(self, db_path: str, max_size_mb: int = 500):
        self.db_path = db_path
        self.max_size_bytes = max_size_mb * 1024 * 1024
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        # Migrace: přidat nové sloupce pro existující DB (idempotentní)
        for col in ["avg_phase_turbulence", "avg_ratio_turbulence", "avg_breathing_score"]:
            try:
                self._conn.execute(
                    f"ALTER TABLE csi_stats_hourly ADD COLUMN {col} REAL")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # sloupec již existuje
        log.info("FusionDatabase opened: %s (max %d MB)", db_path, max_size_mb)

    def close(self):
        self._conn.close()

    def log_event(self, pair: str, event_type: str, state: str,
                  confidence: float, source: str, zone: str,
                  distance_mm: float, fsm_state: str,
                  details: Optional[dict] = None):
        details_json = json.dumps(details) if details else None
        self._conn.execute(
            "INSERT INTO events (timestamp, pair_name, event_type, state, confidence, "
            "source, zone, distance_mm, fsm_state, details_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), pair, event_type, state, confidence, source, zone,
             distance_mm, fsm_state, details_json))
        self._conn.commit()

    def log_calibration(self, node_id: str, threshold: float,
                        idle_n: int, motion_n: int,
                        p80: float = 0.0, p90: float = 0.0):
        self._conn.execute(
            "INSERT INTO calibration_history (timestamp, node_id, threshold, "
            "idle_n, motion_n, p80, p90) VALUES (?,?,?,?,?,?,?)",
            (time.time(), node_id, threshold, idle_n, motion_n, p80, p90))
        self._conn.commit()

    def log_csi_stats(self, node_id: str, avg_variance: float,
                      avg_turbulence: float, avg_amplitude: float,
                      motion_pct: float, packet_count: int,
                      avg_phase_turbulence: float = 0.0,
                      avg_ratio_turbulence: float = 0.0,
                      avg_breathing_score: float = 0.0):
        now = time.time()
        # Round to current hour
        hour_ts = now - (now % 3600)
        self._conn.execute(
            "INSERT INTO csi_stats_hourly (timestamp_hour, node_id, avg_variance, "
            "avg_turbulence, avg_amplitude, motion_pct, packet_count, avg_phase_turbulence, "
            "avg_ratio_turbulence, avg_breathing_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (hour_ts, node_id, avg_variance, avg_turbulence, avg_amplitude,
             motion_pct, packet_count, avg_phase_turbulence,
             avg_ratio_turbulence, avg_breathing_score))
        self._conn.commit()

    def flush_subcarrier_stats(self, node_id: str, analyzer: "SubcarrierAnalyzer"):
        """Persist current per-subcarrier mean+variance from analyzer to DB."""
        now = int(time.time())
        hour_ts = now - (now % 3600)
        for i in range(analyzer.NUM_SC):
            n = len(analyzer._buffers[i])
            if n < 10:
                continue
            variance = analyzer._m2s[i] / n if n > 0 else 0.0
            self._conn.execute(
                "INSERT OR REPLACE INTO subcarrier_stats_hourly "
                "(timestamp, node_id, subcarrier, mean, variance, sample_count) "
                "VALUES (?,?,?,?,?,?)",
                (hour_ts, node_id, i, analyzer._means[i], variance, n))
        self._conn.commit()

    def log_snapshot(self, pair: str, fusion_result, ld2450, ld2412,
                     csi_states: dict, ha_sensors: list):
        """Periodic snapshot of ALL sensor states for offline analysis."""
        now = time.time()
        # LD2450 targets
        ld2450_targets = None
        ld2450_count = 0
        ld2450_dist = 0.0
        if ld2450:
            active = [t for t in ld2450.targets
                      if (t.x != 0 or t.y != 0) and (now - t.last_update) < 2.0]
            ld2450_count = len(active)
            ld2450_targets = json.dumps([{"x": t.x, "y": t.y}
                                         for t in active])
            if active:
                ld2450_dist = min((t.x**2 + t.y**2)**0.5 for t in active)
        # LD2412
        ld2412_pres = 0
        ld2412_dist = 0.0
        ld2412_dir = "none"
        if ld2412 and ld2412.presence:
            ld2412_pres = 1
            ld2412_dist = ld2412.distance_cm
            ld2412_dir = ld2412.direction
        # CSI per-node
        csi_data = {}
        for nid, cst in csi_states.items():
            if not cst.detector_ready:
                continue
            ar = (cst.turbulence / cst.mean_turbulence
                  if cst.mean_turbulence > 0.001
                  else cst.movement_score / max(cst.active_threshold, 0.001))
            csi_data[nid] = {
                "var": round(cst.variance, 4),
                "turb": round(cst.turbulence, 4),
                "ar": round(ar, 2),
                "amp": round(cst.amplitude_sum, 1),
                "thr": round(cst.active_threshold, 4),
                "pt": round(cst.phase_turbulence, 4),
                "rt": round(cst.ratio_turbulence, 4),
                "cs": round(cst.composite_score, 3),
                "ad": round(cst.amplitude_deviation, 3),
                "br": round(cst.breathing_score, 3),
            }
        # HA sensors
        ha_data = {}
        for hs in (ha_sensors or []):
            short = hs.entity_id.split(".")[-1][:30]
            ha_data[short] = {"on": hs.state, "type": hs.sensor_type}
        # Fusion
        f_state = "clear"
        f_conf = 0.0
        f_source = ""
        f_fsm = "clear"
        if fusion_result:
            f_state = "detected" if fusion_result.presence else "clear"
            f_conf = fusion_result.confidence
            f_source = fusion_result.source
            f_fsm = fusion_result.state.value
        self._conn.execute(
            "INSERT INTO sensor_snapshots (timestamp, pair_name, fusion_state, "
            "fusion_confidence, fusion_source, fsm_state, ld2450_target_count, "
            "ld2450_distance_mm, ld2450_targets_json, ld2412_presence, "
            "ld2412_distance_cm, ld2412_direction, csi_json, ha_sensors_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, pair, f_state, f_conf, f_source, f_fsm, ld2450_count,
             ld2450_dist, ld2450_targets, ld2412_pres, ld2412_dist, ld2412_dir,
             json.dumps(csi_data), json.dumps(ha_data)))
        self._conn.commit()

    def log_discovered_node(self, node_id: str, ip: str, zone: str,
                            auto_added: bool = False) -> bool:
        """Upsert discovered node. Returns True if newly discovered."""
        now = time.time()
        cur = self._conn.execute(
            "SELECT node_id FROM discovered_nodes WHERE node_id=?", (node_id,))
        existing = cur.fetchone()
        if existing:
            self._conn.execute(
                "UPDATE discovered_nodes SET last_seen=?, ip=?, active=1 WHERE node_id=?",
                (now, ip, node_id))
            self._conn.commit()
            return False
        else:
            self._conn.execute(
                "INSERT INTO discovered_nodes (node_id, first_seen, last_seen, ip, zone, "
                "auto_added, active) VALUES (?,?,?,?,?,?,1)",
                (node_id, now, now, ip, zone, int(auto_added)))
            self._conn.commit()
            return True

    def log_ground_truth(self, room: str, entity_id: str, sensor_type: str,
                         state: str, csi_node: Optional[str] = None,
                         csi_variance: Optional[float] = None,
                         csi_motion: Optional[int] = None,
                         csi_turbulence: Optional[float] = None,
                         fusion_state: Optional[str] = None,
                         fusion_confidence: Optional[float] = None):
        self._conn.execute(
            "INSERT INTO ha_ground_truth (timestamp, room, entity_id, sensor_type, "
            "state, csi_node, csi_variance, csi_motion, csi_turbulence, "
            "fusion_state, fusion_confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), room, entity_id, sensor_type, state,
             csi_node, csi_variance, csi_motion, csi_turbulence,
             fusion_state, fusion_confidence))
        self._conn.commit()

    def get_ground_truth_stats(self, hours: int = 24) -> dict:
        """Per-room agreement stats: HA ground truth vs CSI detection."""
        cutoff = time.time() - hours * 3600
        stats: dict = {}

        # Per-room: total samples, agreements, false negatives, false positives
        cur = self._conn.execute(
            "SELECT room, state, csi_motion, COUNT(*) "
            "FROM ha_ground_truth WHERE timestamp > ? "
            "GROUP BY room, state, csi_motion",
            (cutoff,))
        rooms: dict = {}
        for room, ha_state, csi_mot, cnt in cur.fetchall():
            r = rooms.setdefault(room, {
                "total": 0, "agree": 0, "fn": 0, "fp": 0,
            })
            r["total"] += cnt
            ha_on = ha_state == "on"
            csi_on = csi_mot == 1
            if ha_on and csi_on:
                r["agree"] += cnt       # both detect
            elif not ha_on and not csi_on:
                r["agree"] += cnt       # both clear
            elif ha_on and not csi_on:
                r["fn"] += cnt          # MW on, CSI missed (false negative)
            elif not ha_on and csi_on:
                r["fp"] += cnt          # MW off, CSI ghost (false positive)

        for room, r in rooms.items():
            total = r["total"]
            stats[room] = {
                "total": total,
                "agreement_pct": round(100.0 * r["agree"] / total, 1) if total else 0,
                "false_neg_pct": round(100.0 * r["fn"] / total, 1) if total else 0,
                "false_pos_pct": round(100.0 * r["fp"] / total, 1) if total else 0,
            }

        return stats

    def cleanup_if_needed(self):
        """Rotate old events and csi_stats if DB exceeds max size."""
        try:
            db_size = os.path.getsize(self.db_path)
        except OSError:
            return
        if db_size <= self.max_size_bytes:
            return

        target = int(self.max_size_bytes * 0.8)
        log.info("DB size %d MB > limit %d MB, cleaning up...",
                 db_size // (1024 * 1024), self.max_size_bytes // (1024 * 1024))

        # Delete oldest rows in chunks until under target
        # Stop when nothing more to delete (prevents infinite loop when file
        # size only shrinks after VACUUM).
        tables = [
            ("events",                 "id", "timestamp",      10000),
            ("sensor_snapshots",       "id", "timestamp",      10000),
            ("csi_stats_hourly",       "id", "timestamp_hour",  1000),
            ("subcarrier_stats_hourly","id", "timestamp",       1000),
            ("ha_ground_truth",        "id", "timestamp",      10000),
        ]
        while db_size > target:
            total_deleted = 0
            for table, id_col, ts_col, chunk in tables:
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE {id_col} IN "
                    f"(SELECT {id_col} FROM {table} "
                    f"ORDER BY {ts_col} ASC LIMIT {chunk})")
                total_deleted += cur.rowcount
            self._conn.commit()
            if total_deleted == 0:
                break
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            try:
                db_size = os.path.getsize(self.db_path)
            except OSError:
                break

        # VACUUM requires autocommit mode (no open transaction)
        prev_iso = self._conn.isolation_level
        self._conn.isolation_level = None
        try:
            self._conn.execute("VACUUM")
            log.info("DB cleanup done, new size: %d MB",
                     os.path.getsize(self.db_path) // (1024 * 1024))
        except Exception as e:
            log.warning("VACUUM failed: %s", e)
        finally:
            self._conn.isolation_level = prev_iso

    def get_stats(self, hours: int = 24) -> dict:
        """Get summary statistics for the last N hours."""
        cutoff = time.time() - hours * 3600
        stats = {}

        # Event counts by type
        cur = self._conn.execute(
            "SELECT event_type, COUNT(*) FROM events WHERE timestamp > ? GROUP BY event_type",
            (cutoff,))
        stats["events"] = {row[0]: row[1] for row in cur.fetchall()}

        # Total detections
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE timestamp > ? AND state='detected'",
            (cutoff,))
        stats["detections_24h"] = cur.fetchone()[0]

        # Average confidence when detected
        cur = self._conn.execute(
            "SELECT AVG(confidence) FROM events WHERE timestamp > ? AND state='detected'",
            (cutoff,))
        avg = cur.fetchone()[0]
        stats["avg_confidence"] = round(avg, 2) if avg else 0.0

        # CSI stats per node (latest hour)
        cur = self._conn.execute(
            "SELECT node_id, avg_variance, avg_turbulence, motion_pct, packet_count "
            "FROM csi_stats_hourly WHERE timestamp_hour > ? ORDER BY timestamp_hour DESC",
            (cutoff,))
        csi_stats = {}
        for row in cur.fetchall():
            if row[0] not in csi_stats:  # Keep only latest per node
                csi_stats[row[0]] = {
                    "avg_variance": round(row[1], 4) if row[1] else 0,
                    "avg_turbulence": round(row[2], 4) if row[2] else 0,
                    "motion_pct": round(row[3], 1) if row[3] else 0,
                    "packets": row[4] or 0,
                }
        stats["csi"] = csi_stats

        # Discovered nodes
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM discovered_nodes WHERE active=1")
        stats["active_nodes"] = cur.fetchone()[0]

        # DB size
        try:
            stats["db_size_mb"] = round(os.path.getsize(self.db_path) / (1024 * 1024), 1)
        except OSError:
            stats["db_size_mb"] = 0

        return stats

    def get_discovered_nodes(self) -> list[dict]:
        """Return all discovered nodes."""
        cur = self._conn.execute(
            "SELECT node_id, first_seen, last_seen, ip, zone, auto_added, active "
            "FROM discovered_nodes ORDER BY last_seen DESC")
        return [
            {"node_id": r[0], "first_seen": r[1], "last_seen": r[2],
             "ip": r[3], "zone": r[4], "auto_added": bool(r[5]), "active": bool(r[6])}
            for r in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# 4-State Machine with hysteresis
# ---------------------------------------------------------------------------

class PresenceStateMachine:
    """
    4-state presence detection with debounce and anti-pingpong:
      CLEAR → DEBOUNCING (motion detected, confirm for debounce_in_s)
      DEBOUNCING → PRESENT (confirmed)
      DEBOUNCING → CLEAR (false alarm, motion stopped before confirm)
      PRESENT → CLEARING (motion lost, wait clearing_s before going CLEAR)
      CLEARING → PRESENT (motion returned during clearing window)
      CLEARING → CLEAR (confirmed absence)

    Anti-pingpong: minimum present duration prevents rapid oscillation.
    Data analysis showed 925 ping-pong cycles in obyvak over 33 days,
    caused by LD2450 intermittent detection dropping confidence briefly.
    """

    def __init__(self, debounce_in_s: float = 1.0, clearing_s: float = 5.0,
                 min_present_s: float = 30.0):
        self.debounce_in_s = debounce_in_s
        self.clearing_s = clearing_s
        self.min_present_s = min_present_s
        self.state = FusionState.CLEAR
        self._transition_time: float = 0.0
        self._present_since: float = 0.0  # when PRESENT was first entered

    def update(self, detected: bool, now: float = None) -> FusionState:
        now = now or time.time()

        if self.state == FusionState.CLEAR:
            if detected:
                self.state = FusionState.DEBOUNCING
                self._transition_time = now

        elif self.state == FusionState.DEBOUNCING:
            if not detected:
                self.state = FusionState.CLEAR
            elif (now - self._transition_time) >= self.debounce_in_s:
                self.state = FusionState.PRESENT
                self._present_since = now

        elif self.state == FusionState.PRESENT:
            if not detected:
                # Anti-pingpong: stay PRESENT for at least min_present_s
                if (now - self._present_since) >= self.min_present_s:
                    self.state = FusionState.CLEARING
                    self._transition_time = now
                # else: ignore brief confidence drops

        elif self.state == FusionState.CLEARING:
            if detected:
                self.state = FusionState.PRESENT
                # Don't reset _present_since — continuous presence session
            elif (now - self._transition_time) >= self.clearing_s:
                self.state = FusionState.CLEAR

        return self.state

    @property
    def is_present(self) -> bool:
        return self.state in (FusionState.PRESENT, FusionState.CLEARING)


# ---------------------------------------------------------------------------
# Fusion Engine v2
# ---------------------------------------------------------------------------

class FusionEngine:
    def __init__(self, threshold: float = 0.7, max_latency_ms: int = 2000,
                 debounce_in_s: float = 1.0, clearing_s: float = 5.0,
                 ld2450_solo_penalty: float = 0.0,
                 min_present_s: float = 30.0,
                 exit_threshold: float = 0.25):
        self.threshold = threshold
        self.exit_threshold = exit_threshold
        self.max_latency_ms = max_latency_ms
        self.ld2450_solo_penalty = ld2450_solo_penalty

        # Z-score analyzers per data stream
        self.z_energy_mov = ZScoreAnalyzer(window_size=60, z_threshold=2.0)
        self.z_energy_stat = ZScoreAnalyzer(window_size=60, z_threshold=2.0)
        self.z_csi_score = ZScoreAnalyzer(window_size=30, z_threshold=2.5)
        self.z_phase_turb = ZScoreAnalyzer(window_size=60, z_threshold=2.0)

        # LD2412 distance jitter detection: rolling buffer of last 5 readings (mm)
        self._ld2412_dist_buf: collections.deque = collections.deque(maxlen=5)

        # 4-state machine with anti-pingpong
        self.state_machine = PresenceStateMachine(
            debounce_in_s=debounce_in_s,
            clearing_s=clearing_s,
            min_present_s=min_present_s,
        )

    def _is_stale(self, state_ts: float) -> bool:
        return (time.time() - state_ts) > (self.max_latency_ms / 1000.0)

    @staticmethod
    def _ld2450_distance_weight(closest_mm: float, ld2412_present: bool,
                                ld2412_dist_mm: float = 0.0) -> float:
        """Return confidence weight for LD2450 based on target distance.

        Phantom targets cluster at 3-4m; reduce weight in that zone.
        Apply an extra penalty when LD2412 is present but its measured
        distance differs greatly from LD2450 (signals don't agree → phantom).
        """
        if closest_mm < 1500:
            weight = 0.40
        elif closest_mm < 2500:
            weight = 0.35
        elif closest_mm < 3500:
            weight = 0.25   # phantom zone
        elif closest_mm < 4500:
            weight = 0.20
        else:
            weight = 0.12   # very likely phantom

        if ld2412_present and ld2412_dist_mm > 0:
            if abs(ld2412_dist_mm - closest_mm) > 1500:
                weight -= 0.10  # disagree → phantom penalty

        return max(weight, 0.0)

    def fuse(self, ld2412: Optional[LD2412State], ld2450: Optional[LD2450State],
             csi: Optional[CSIState] = None,
             dual_csi: Optional[DualCSIResult] = None,
             amplitude_blocked: bool = False,
             ha_sensors: Optional[list] = None) -> FusionResult:
        """Multi-layer fusion with z-score analysis and state machine."""
        now = time.time()
        confidence = 0.0
        sources = []
        distance_mm = 0.0
        reason_parts = []

        ld2412_stale = ld2412 is None or self._is_stale(ld2412.last_update)
        ld2450_stale = ld2450 is None or self._is_stale(ld2450.last_update)
        csi_stale = csi is None or self._is_stale(csi.last_update)

        ld2412_present = ld2412 is not None and ld2412.presence and not ld2412_stale
        ld2450_present = ld2450 is not None and ld2450.presence and not ld2450_stale
        csi_motion = (not csi_stale) and csi.motion

        active_targets = [t for t in ld2450.targets
                         if (t.x != 0 or t.y != 0) and (now - t.last_update) < 2.0
                         ] if ld2450 else []
        # Cap to firmware-reported count if available (ghost-filtered)
        if ld2450 and ld2450.target_count > 0 and len(active_targets) > ld2450.target_count:
            active_targets = active_targets[:ld2450.target_count]

        # CSI-only mode: boost weights when no radars configured
        csi_only = ld2412 is None and ld2450 is None
        csi_weight_strong = 0.75 if csi_only else 0.35
        csi_weight_normal = 0.65 if csi_only else 0.3
        csi_weight_weak = 0.5 if csi_only else 0.2

        # --- Layer 1: LD2412 1D radar ---
        # Per-sensor confidence: 0.0-1.0 (quality of this sensor's detection)
        # Weight: importance of this sensor in the weighted average
        sensor_scores = []  # (name, weight, sensor_confidence)

        if ld2412_present:
            ld2412_conf = 0.7  # base presence confidence
            sources.append("ld2412")
            reason_parts.append(f"ld2412(d={ld2412.distance_cm}cm)")

            # Z-score bonus for energy anomaly
            if not ld2412_stale and self.z_energy_mov.calibrated:
                z_mov = self.z_energy_mov.update(float(ld2412.moving_energy))
                if abs(z_mov) > 2.0:
                    ld2412_conf = min(ld2412_conf + 0.2, 1.0)
                    reason_parts.append(f"z_mov={z_mov:.1f}")

            # Distance jitter penalty: wildly varying distances → phantom
            self._ld2412_dist_buf.append(ld2412.distance_cm * 10.0)
            if len(self._ld2412_dist_buf) >= 5:
                vals = list(self._ld2412_dist_buf)
                mean = sum(vals) / len(vals)
                stddev = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
                if stddev > 1500.0:
                    ld2412_conf = max(ld2412_conf - 0.3, 0.1)
                    reason_parts.append(f"ld2412_jitter={stddev:.0f}mm")

            sensor_scores.append(("ld2412", 0.50, ld2412_conf))

        elif ld2412 is not None and not ld2412_stale:
            # Feed baseline even when idle
            self.z_energy_mov.update(float(ld2412.moving_energy))
            self.z_energy_stat.update(float(ld2412.static_energy))

        # --- Layer 2: LD2450 2D radar ---
        if ld2450_present and active_targets:
            sources.append("ld2450")
            ld2450_closest_mm = min(t.distance_mm for t in active_targets)
            ld2412_dist_mm = (ld2412.distance_cm * 10.0
                              if ld2412_present and not ld2412_stale else 0.0)
            ld2450_w = self._ld2450_distance_weight(
                ld2450_closest_mm, ld2412_present and not ld2412_stale, ld2412_dist_mm)
            # Convert distance weight (0.0-0.40) to sensor confidence (0.0-1.0)
            ld2450_conf = min(ld2450_w / 0.40, 1.0)
            reason_parts.append(
                f"ld2450(n={len(active_targets)},d={ld2450_closest_mm:.0f}mm,c={ld2450_conf:.2f})")

            # Solo penalty: reduce confidence when LD2412 can't cross-validate
            if self.ld2450_solo_penalty > 0 and not ld2412_present:
                ld2450_conf = max(ld2450_conf - 0.3, 0.1)
                reason_parts.append(f"ld2450_solo_pen")

            sensor_scores.append(("ld2450", 0.35, ld2450_conf))

        # --- Layer 3: WiFi CSI (multi-feature scoring) ---
        # Data analysis (470k snapshots) showed anomaly_ratio ≈ 1.0 for both
        # detected and clear states — no discriminative power. New approach:
        # combine variance, phase_turbulence, z-scores, and amplitude for
        # a composite CSI confidence instead of relying on ar alone.
        csi_conf = 0.0
        csi_reasons = []

        if not csi_stale and csi is not None and not csi.calibrating:
            # Always feed z-score analyzers (even when not in motion)
            z_csi = (self.z_csi_score.update(csi.movement_score)
                     if self.z_csi_score.calibrated
                     else (self.z_csi_score.update(csi.movement_score) or 0.0))
            z_phase = 0.0
            if csi.phase_turbulence > 0.001:
                z_phase = (self.z_phase_turb.update(csi.phase_turbulence)
                           if self.z_phase_turb.calibrated
                           else (self.z_phase_turb.update(csi.phase_turbulence) or 0.0))

            if csi_motion:
                # Primary: on-chip composite score (4-domain weighted from idle baselines)
                # Falls back to variance ratio if composite not available
                if csi.composite_score > 0.01:
                    # Composite score is unbounded above; map to 0-1 confidence
                    # Typical values: 0.05-0.5 idle noise, 0.5-5.0 motion
                    cs = csi.composite_score
                    if cs > 2.0:
                        csi_conf += 0.25
                        csi_reasons.append(f"cs_strong={cs:.2f}")
                    elif cs > 0.5:
                        csi_conf += 0.15
                        csi_reasons.append(f"cs_mod={cs:.2f}")
                    elif cs > 0.1:
                        csi_conf += 0.08
                        csi_reasons.append(f"cs_weak={cs:.2f}")
                elif csi.variance > 0 and csi.active_threshold > 0:
                    # Fallback: variance ratio (composite not yet available)
                    var_ratio = csi.variance / csi.active_threshold
                    if var_ratio > 3.0:
                        csi_conf += 0.20
                        csi_reasons.append(f"var_strong={var_ratio:.1f}")
                    elif var_ratio > 1.5:
                        csi_conf += 0.12
                        csi_reasons.append(f"var_mod={var_ratio:.1f}")
                    elif var_ratio > 0.8:
                        csi_conf += 0.05
                        csi_reasons.append(f"var_weak={var_ratio:.1f}")

                # Supplementary: z-score anomaly on movement score
                if self.z_csi_score.calibrated and z_csi is not None and z_csi > 2.5:
                    csi_conf += 0.08
                    csi_reasons.append(f"z_csi={z_csi:.1f}")

                # Supplementary: breathing score (stationary presence indicator)
                if csi.breathing_score > 0.1:
                    csi_conf += 0.06
                    csi_reasons.append(f"breath={csi.breathing_score:.2f}")

                if csi_conf > 0:
                    sources.append("csi")
                    # Normalize to 0-1 (max raw csi_conf ≈ 0.39)
                    csi_sensor_conf = min(csi_conf / 0.39, 1.0)
                    csi_weight = 0.50 if csi_only else 0.25
                    sensor_scores.append(("csi", csi_weight, csi_sensor_conf))
                    csi_reasons.insert(0, f"csi_c={csi_sensor_conf:.2f}")

            reason_parts.extend(csi_reasons)

            # Amplitude-based static presence: require cross-validation with
            # at least one other active source to avoid 98k false positives
            # (analysis showed amplitude_blocked was the #1 false positive source)
            # Amplitude-based static presence: only count if cross-validated
            if amplitude_blocked and not csi_motion and len(sources) >= 1:
                sources.append("csi_static")
                sensor_scores.append(("csi_static", 0.15, 0.6))
                reason_parts.append("amplitude_blocked+confirmed")
        else:
            # Feed baseline even when stale/calibrating
            if csi is not None:
                self.z_csi_score.update(csi.movement_score)
                if csi.phase_turbulence > 0.001:
                    self.z_phase_turb.update(csi.phase_turbulence)

        # --- Layer 4: HA sensors (MW, PIR, camera) ---
        if ha_sensors:
            for hs in ha_sensors:
                if not hs.state or (now - hs.last_update) > 30.0:
                    continue
                # Skip stuck sensors: ON for > stuck_timeout_s without state change
                age = (now - hs.state_since) if hs.state_since > 0 else 0
                if hs.state_since > 0 and age > hs.stuck_timeout_s:
                    if not getattr(hs, '_stuck_logged', False):
                        short_s = hs.entity_id.split(".")[-1][:25]
                        log.warning("Stuck sensor filtered: %s (on for %.0fs)", short_s, age)
                        hs._stuck_logged = True
                    continue
                short = hs.entity_id.split(".")[-1][:20]
                sources.append(f"ha_{hs.sensor_type}")
                # HA sensor confidence: high (0.85) unless in warmup (0.5)
                ha_conf = 0.85
                if hs.warmup_s > 0 and hs.state_since > 0 and age < hs.warmup_s:
                    ha_conf = 0.5
                    reason_parts.append(f"mw_warmup({age:.0f}s)")
                sensor_scores.append((f"ha_{hs.sensor_type}", hs.weight, ha_conf))
                reason_parts.append(f"ha({short}={hs.sensor_type})")

        # --- Weighted average confidence from all sensor scores ---
        n_sources = len(sources)
        if sensor_scores:
            total_weight = sum(w for _, w, _ in sensor_scores)
            confidence = sum(w * c for _, w, c in sensor_scores) / max(total_weight, 0.01)

            # Cross-validation bonus: multiple independent sensors agreeing
            if n_sources >= 3:
                confidence = min(confidence * 1.25, 1.0)
                reason_parts.append("triple_confirm")
            elif n_sources >= 2:
                confidence = min(confidence * 1.15, 1.0)
                reason_parts.append("dual_confirm")

            reason_parts.append(
                f"scores=[{','.join(f'{n}:{c:.2f}' for n, _, c in sensor_scores)}]")
        # else: confidence stays 0.0 (no sensors detected anything)

        # --- Dual-CSI adjustment ---
        active_zone = "none"
        movement_direction = "none"
        if dual_csi is not None:
            active_zone = dual_csi.active_zone
            movement_direction = dual_csi.movement_direction
            # Scale dual-CSI adjustment (was additive, now proportional)
            if dual_csi.is_noise:
                confidence *= 0.85
                reason_parts.append("csi_noise")
            elif dual_csi.active_zone == "transition":
                confidence = min(confidence * 1.15, 1.0)
                reason_parts.append(f"transition({dual_csi.movement_direction})")
            elif dual_csi.active_zone != "none":
                confidence = min(confidence * 1.08, 1.0)
                reason_parts.append(f"zone={dual_csi.active_zone}")

        # --- Distance calculation ---
        if ld2412_present and active_targets:
            ld2412_mm = ld2412.distance_cm * 10.0
            closest_mm = min(t.distance_mm for t in active_targets)
            distance_mm = (ld2412_mm + closest_mm) / 2.0
            # Distance agreement boosts confidence
            if abs(ld2412_mm - closest_mm) < 500:
                confidence = min(confidence * 1.05, 1.0)
        elif ld2412_present:
            distance_mm = ld2412.distance_cm * 10.0
        elif active_targets:
            distance_mm = min(t.distance_mm for t in active_targets)

        confidence = round(max(min(confidence, 1.0), 0.0), 2)

        # --- Alarm state forwarding ---
        alarm_state = "disarmed"
        alarm_sources = []
        if ld2412 is not None:
            alarm_sources.append(ld2412.alarm_state)
        if ld2450 is not None:
            alarm_sources.append(ld2450.alarm_state)
        for st in alarm_sources:
            if st in ("triggered", "armed_away", "armed_home", "pending", "arming"):
                alarm_state = st
                break

        # --- State machine with hysteresis ---
        # Enter PRESENT at high threshold (0.7), stay until conf drops below exit (0.3)
        if self.state_machine.state in (FusionState.PRESENT, FusionState.CLEARING):
            raw_detected = confidence >= self.exit_threshold
        else:
            raw_detected = confidence >= self.threshold
        fsm_state = self.state_machine.update(raw_detected, now)
        presence = self.state_machine.is_present

        source_str = "+".join(sources) if sources else "none"

        return FusionResult(
            presence=presence,
            confidence=confidence,
            source=source_str,
            distance_mm=round(distance_mm, 0),
            alarm_state=alarm_state,
            state=fsm_state,
            reason="; ".join(reason_parts) if reason_parts else "idle",
            active_zone=active_zone,
            movement_direction=movement_direction,
        )


# ---------------------------------------------------------------------------
# Tapo Camera Controller
# ---------------------------------------------------------------------------

class TapoCameraController:
    """
    Non-blocking PTZ controller for Tapo cameras via pytapo.
    Triggered by fusion presence/zone changes.

    Config example (config.yaml):
      tapo_cameras:
        - ip: 192.168.1.50
          user: your-tapo-account@example.com
          password: "YOUR_TAPO_PASSWORD"
          pair: living_hallway       # fusion pair that triggers this camera
          zone_presets:              # active_zone → preset ID
            living: "1"
            hallway: "2"
          default_preset: "1"        # preset when zone is unknown/none
          min_confidence: 0.6        # ignore low-confidence detections
    """

    def __init__(self, cameras_cfg: list):
        self._cameras = cameras_cfg   # list of dicts from config
        self._lock = threading.Lock()
        self._last_preset: dict[str, str] = {}   # ip → last preset ID sent
        self._fail_count: dict[str, int] = {}    # ip → consecutive failures
        self._next_retry: dict[str, float] = {}  # ip → earliest retry timestamp
        self._log = logging.getLogger("TapoController")
        # Verify pytapo available
        try:
            import pytapo  # noqa: F401
            self._available = True
        except ImportError:
            self._log.warning("pytapo not installed — Tapo PTZ disabled. Run: pip install pytapo")
            self._available = False

    def on_fusion_result(self, pair_name: str, result) -> None:
        """Called from _publish_fusion — triggers PTZ if presence detected."""
        if not self._available:
            return
        now = time.time()
        for cam_cfg in self._cameras:
            if cam_cfg.get("pair") != pair_name:
                continue
            if not result.presence:
                continue
            min_conf = cam_cfg.get("min_confidence", 0.6)
            if result.confidence < min_conf:
                continue
            ip = cam_cfg["ip"]
            # Backoff: skip if camera is failing and retry time not reached
            if now < self._next_retry.get(ip, 0):
                return
            # Auto-disable after 10 consecutive failures (retry every 30 min)
            if self._fail_count.get(ip, 0) >= 10:
                if now < self._next_retry.get(ip, 0):
                    return
                self._log.info("Tapo %s: retrying after %d failures",
                               ip, self._fail_count[ip])
            zone = result.active_zone or "none"
            zone_presets = cam_cfg.get("zone_presets", {})
            preset_id = zone_presets.get(zone, cam_cfg.get("default_preset", "1"))
            # Skip if already on this preset
            with self._lock:
                if self._last_preset.get(ip) == preset_id:
                    continue
                self._last_preset[ip] = preset_id
            # Move in background thread — don't block fusion loop
            t = threading.Thread(
                target=self._move_camera,
                args=(cam_cfg, preset_id, zone),
                daemon=True,
            )
            t.start()

    def _move_camera(self, cam_cfg: dict, preset_id: str, zone: str) -> None:
        ip = cam_cfg["ip"]
        try:
            from pytapo import Tapo
            cam = Tapo(ip, cam_cfg["user"], cam_cfg["password"], cam_cfg["password"])
            cam.getPresets()   # required before setPreset
            cam.setPreset(preset_id)
            self._log.info("Tapo %s → preset %s (zone=%s)", ip, preset_id, zone)
            # Success — reset failure state
            self._fail_count[ip] = 0
            self._next_retry.pop(ip, None)
            try:
                cam.close()
            except Exception:
                pass
        except Exception as exc:
            fails = self._fail_count.get(ip, 0) + 1
            self._fail_count[ip] = fails
            # Exponential backoff: 30s, 60s, 120s, ... max 1800s (30 min)
            backoff = min(30 * (2 ** (fails - 1)), 1800)
            self._next_retry[ip] = time.time() + backoff
            if fails <= 3 or fails % 10 == 0:
                self._log.warning("Tapo %s failed (%d×, next retry in %ds): %s",
                                  ip, fails, backoff, exc)
            # Reset last_preset so next event retries
            with self._lock:
                self._last_preset.pop(ip, None)


# ---------------------------------------------------------------------------
# Telegram Notifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Sends alerts + receives commands via Telegram Bot API (urllib, no extra deps)."""

    API_URL = "https://api.telegram.org/bot{token}/{method}"

    COMMANDS = {
        "/blokuji": "mute",
        "/odblokuji": "unmute",
        "/stav": "status",
        "/stats": "stats",
        "/gt": "ground_truth",
        "/arm": "arm_away",
        "/disarm": "disarm",
        "/arm_now": "arm_now",
        "/arm_home": "arm_home",
        "/help": "help",
        "/start": "help",
    }

    def __init__(self, token: str, chat_id: str, cooldown_s: float = 30.0,
                 poll_interval: float = 5.0):
        self.token = token
        self.chat_id = chat_id
        self.cooldown_s = cooldown_s
        self._last_send: float = 0.0
        self._enabled = bool(token and chat_id)
        self.muted = False
        self._last_poll: float = 0.0
        self._poll_interval = poll_interval
        self._update_offset: int = 0
        self._status_callback = None  # set by FusionService
        self._stats_callback = None   # set by FusionService (for /stats)
        self._gt_callback = None      # set by FusionService (for /gt)
        self._mqtt_client = None      # set by FusionService
        self._alarm_topic = ""        # set by FusionService
        if self._enabled:
            log.info("Telegram notifications enabled (chat_id=%s)", chat_id)
        else:
            log.info("Telegram notifications disabled (no token/chat_id)")

    def send(self, text: str, force: bool = False) -> bool:
        if not self._enabled:
            return False
        if self.muted and not force:
            return False
        now = time.time()
        if not force and (now - self._last_send) < self.cooldown_s:
            return False
        try:
            url = self.API_URL.format(token=self.token, method="sendMessage")
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status == 200
            if ok:
                self._last_send = now
                log.info("Telegram sent: %s", text[:60])
            return ok
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return False

    def poll_commands(self):
        """Poll for incoming Telegram commands. Call from main loop."""
        if not self._enabled:
            return
        now = time.time()
        if (now - self._last_poll) < self._poll_interval:
            return
        self._last_poll = now
        try:
            url = self.API_URL.format(token=self.token, method="getUpdates")
            params = urllib.parse.urlencode({
                "offset": self._update_offset,
                "timeout": 0,
                "limit": 10,
            })
            req = urllib.request.Request(f"{url}?{params}", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            if not data.get("ok"):
                return
            for update in data.get("result", []):
                self._update_offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != self.chat_id:
                    continue
                text = msg.get("text", "").strip().lower()
                cmd = text.split()[0] if text else ""
                if cmd in self.COMMANDS:
                    log.info("Telegram command received: %s", cmd)
                    self._handle_command(self.COMMANDS[cmd])
                elif cmd:
                    log.info("Telegram unknown command: %s", cmd)
        except Exception as e:
            log.warning("Telegram poll failed: %s", e)

    def _handle_command(self, action: str):
        if action == "mute":
            self.muted = True
            self.send("🔇 <b>Notifikace ztlumeny</b>\n/odblokuji pro obnoveni", force=True)
            log.info("Telegram muted by user command")
        elif action == "unmute":
            self.muted = False
            self.send("🔔 <b>Notifikace obnoveny</b>", force=True)
            log.info("Telegram unmuted by user command")
        elif action == "status":
            status_text = "🔇 ztlumeno" if self.muted else "🔔 aktivni"
            extra = ""
            if self._status_callback:
                extra = self._status_callback()
            self.send(
                f"📊 <b>Stav fusion</b>\nNotifikace: {status_text}{extra}",
                force=True,
            )
        elif action == "stats":
            if self._stats_callback:
                stats_text = self._stats_callback()
                self.send(stats_text, force=True)
            else:
                self.send("📊 Statistiky nejsou k dispozici", force=True)
        elif action == "ground_truth":
            if self._gt_callback:
                gt_text = self._gt_callback()
                self.send(gt_text, force=True)
            else:
                self.send("📊 Ground truth neni nakonfigurovano", force=True)
        elif action in ("arm_away", "arm_now", "arm_home", "disarm"):
            if not self._mqtt_client or not self._alarm_topic:
                self.send("⚠️ MQTT nepripojeno", force=True)
                return
            cmd_map = {
                "arm_away": "ARM_AWAY",
                "arm_now": "ARM_AWAY",
                "arm_home": "ARM_HOME",
                "disarm": "DISARM",
            }
            mqtt_cmd = cmd_map[action]
            self._mqtt_client.publish(self._alarm_topic, mqtt_cmd)
            emoji = "🔒" if action != "disarm" else "🔓"
            label = {"arm_away": "ARM AWAY", "arm_now": "ARM AWAY (instant)",
                     "arm_home": "ARM HOME", "disarm": "DISARMED"}[action]
            self.send(f"{emoji} <b>{label}</b>", force=True)
            log.info("Telegram command: %s → %s", action, mqtt_cmd)
        elif action == "help":
            self.send(
                "📋 <b>Fusion Service</b>\n\n"
                "/arm — aktivovat alarm\n"
                "/arm_home — rezim doma\n"
                "/arm_now — okamzita aktivace\n"
                "/disarm — deaktivovat\n"
                "/blokuji — ztlumit notifikace\n"
                "/odblokuji — obnovit notifikace\n"
                "/stav — stav systemu\n"
                "/stats — detailni statistiky (24h)\n"
                "/gt — ground truth presnost (24h)",
                force=True,
            )


# ---------------------------------------------------------------------------
# HA Ground Truth Poller
# ---------------------------------------------------------------------------

class HAGroundTruthPoller:
    """Polls Home Assistant REST API for reference sensor states (ground truth)."""

    # Room → CSI node mapping
    ROOM_CSI_MAP = {
        "obyvak": "csi_obyvak",
        "kuchyn": "csi_kuchyn",
        "chodba": "csi_chodba",
    }

    def __init__(self, ha_config: dict, db: 'FusionDatabase',
                 csi_states: dict, last_results: dict):
        self._url = ha_config["url"].rstrip("/")
        token = os.environ.get(ha_config.get("token_env", "HA_TOKEN"), "")
        if not token:
            raise ValueError(
                f"HA token not found in env var '{ha_config.get('token_env', 'HA_TOKEN')}'")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._poll_interval = ha_config.get("poll_interval_s", 2.0)
        self._periodic_log_s = ha_config.get("periodic_log_s", 30.0)
        self._sensors: dict[str, list[dict]] = ha_config.get("ground_truth_sensors", {})
        self._db = db
        self._csi_states = csi_states
        self._last_results = last_results

        # entity_id → last known state ("on"/"off")
        self._prev_states: dict[str, str] = {}
        self._last_poll: float = 0.0
        self._last_periodic_log: float = 0.0

        # Active sensor states for fusion (entity_id → HASensorInput)
        self.active_states: dict[str, HASensorInput] = {}

        # Build entity→room lookup
        self._entity_room: dict[str, str] = {}
        self._entity_type: dict[str, str] = {}
        for room, sensors in self._sensors.items():
            for s in sensors:
                eid = s["entity_id"]
                self._entity_room[eid] = room
                self._entity_type[eid] = s.get("type", "unknown")

        n_entities = len(self._entity_room)
        n_rooms = len(self._sensors)
        log.info("HA ground truth: %d entities across %d rooms", n_entities, n_rooms)

    def poll(self):
        """Poll HA states. Call from main loop."""
        now = time.time()
        if (now - self._last_poll) < self._poll_interval:
            return
        self._last_poll = now

        try:
            states = self._fetch_states()
        except Exception as e:
            log.debug("HA poll failed: %s", e)
            return

        # Build entity_id → (state, last_changed_ts) map from HA response
        ha_states: dict[str, tuple] = {}
        for s in states:
            eid = s.get("entity_id", "")
            if eid in self._entity_room or eid in self.active_states:
                # Parse last_changed ISO timestamp to epoch
                lc_ts = 0.0
                lc = s.get("last_changed", "")
                if lc:
                    try:
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(lc)
                        lc_ts = dt.timestamp()
                    except Exception:
                        pass
                ha_states[eid] = (s.get("state", "unknown"), lc_ts)

        periodic = (now - self._last_periodic_log) >= self._periodic_log_s

        for eid, (current_state, last_changed_ts) in ha_states.items():
            # Handle event entities: state is ISO timestamp of last event
            # Convert to on/off: "on" if event within per-sensor window, else "off"
            if eid.startswith("event."):
                window = (self.active_states[eid].event_window_s
                          if eid in self.active_states else 120.0)
                if last_changed_ts > 0 and (now - last_changed_ts) < window:
                    current_state = "on"
                else:
                    current_state = "off"
            elif current_state not in ("on", "off"):
                continue

            prev = self._prev_states.get(eid)
            changed = prev != current_state

            # Ground truth logging (only for entities in _entity_room)
            if eid in self._entity_room:
                if changed or periodic:
                    self._log_snapshot(eid, current_state)
                if changed:
                    room = self._entity_room[eid]
                    stype = self._entity_type[eid]
                    log.info("HA GT [%s] %s (%s): %s → %s",
                             room, eid.split(".")[-1], stype,
                             prev or "?", current_state)

            self._prev_states[eid] = current_state

            # Update active sensor state for fusion
            if eid in self.active_states:
                new_state = (current_state == "on")
                if self.active_states[eid].state != new_state:
                    # On first poll, use HA's last_changed for accurate timing
                    if self.active_states[eid].state_since == 0.0 and last_changed_ts > 0:
                        self.active_states[eid].state_since = last_changed_ts
                    else:
                        self.active_states[eid].state_since = now
                self.active_states[eid].state = new_state
                self.active_states[eid].last_update = now

        if periodic:
            self._last_periodic_log = now

    def _fetch_states(self) -> list[dict]:
        """GET /api/states/<entity_id> pro každou potřebnou entitu — místo bulk /api/states."""
        needed = set(self._entity_room.keys()) | set(self.active_states.keys())
        results = []
        for eid in needed:
            url = f"{self._url}/api/states/{urllib.parse.quote(eid, safe='')}"
            req = urllib.request.Request(url, headers=self._headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    results.append(json.loads(resp.read()))
            except Exception as e:
                log.debug("HA fetch failed for %s: %s", eid, e)
        return results

    def _log_snapshot(self, entity_id: str, state: str):
        """Log one ground truth sample with current CSI/fusion snapshot."""
        room = self._entity_room[entity_id]
        sensor_type = self._entity_type[entity_id]
        csi_node = self.ROOM_CSI_MAP.get(room)

        csi_variance = None
        csi_motion = None
        csi_turbulence = None
        if csi_node:
            csi_st = self._csi_states.get(csi_node)
            if csi_st and csi_st.last_update > 0:
                csi_variance = csi_st.variance
                csi_motion = 1 if csi_st.motion else 0
                csi_turbulence = csi_st.turbulence

        fusion_state = None
        fusion_confidence = None
        # Find fusion result for the pair that contains this CSI node
        for pair_name, result in self._last_results.items():
            if room in pair_name or pair_name in room:
                fusion_state = result.state.value
                fusion_confidence = result.confidence
                break
        # Fallback: use first pair result (single-pair setups)
        if fusion_state is None and self._last_results:
            first_result = next(iter(self._last_results.values()))
            fusion_state = first_result.state.value
            fusion_confidence = first_result.confidence

        self._db.log_ground_truth(
            room=room, entity_id=entity_id, sensor_type=sensor_type,
            state=state, csi_node=csi_node,
            csi_variance=csi_variance, csi_motion=csi_motion,
            csi_turbulence=csi_turbulence,
            fusion_state=fusion_state, fusion_confidence=fusion_confidence,
        )


# ---------------------------------------------------------------------------
# Fusion Service (MQTT glue)
# ---------------------------------------------------------------------------

class FusionService:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self._running = True
        self._startup_time = time.time()

        # State per device_id
        self.ld2412_states: dict[str, LD2412State] = {}
        self.ld2450_states: dict[str, LD2450State] = {}
        self.csi_states: dict[str, CSIState] = {}

        # Engines per pair
        self.engines: dict[str, FusionEngine] = {}
        self.dual_csi_analyzers: dict[str, DualCSIAnalyzer] = {}
        self.last_results: dict[str, FusionResult] = {}
        self._adaptive_threshold_last: dict[str, float] = {}  # csi_id → last publish time
        self.csi_calibration: dict[str, CSICalibrationModel] = {}
        self._csi_continuity_count: dict[str, int] = {}
        self.amplitude_trackers: dict[str, AmplitudeBaselineTracker] = {}
        self._packet_rate_last: dict[str, tuple] = {}  # csi_id → (time, packets_total)
        self._filter_adjust_last: dict[str, float] = {}  # csi_id → last publish time
        self.subcarrier_analyzers: dict[str, SubcarrierAnalyzer] = {}
        self._ha_sensor_map: dict[str, list[str]] = {}  # pair_name → [entity_ids]

        for pair in self.config["pairs"]:
            name = pair["name"]
            ld2412_id = pair.get("ld2412_id", "")
            ld2450_id = pair.get("ld2450_id", "")
            if ld2412_id:
                self.ld2412_states[ld2412_id] = LD2412State(device_id=ld2412_id)
            if ld2450_id:
                self.ld2450_states[ld2450_id] = LD2450State(device_id=ld2450_id)

            csi_ids = pair.get("csi_ids", [])
            for csi_id in csi_ids:
                self.csi_states[csi_id] = CSIState(device_id=csi_id)

            # Dual-CSI analyzer (needs zone map from config)
            csi_zones = pair.get("csi_zones", {})
            if not csi_zones and len(csi_ids) >= 1:
                # Auto-generate zone map from csi_ids: csi_obyvak → obyvak
                csi_zones = {cid: cid.replace("csi_", "") for cid in csi_ids}
            noise_diff = pair.get("noise_diff_threshold", 0.3)
            score_norm = pair.get("score_norm_max", 0.5)
            trans_window = pair.get("transition_window_s", 5.0)
            self.dual_csi_analyzers[name] = DualCSIAnalyzer(
                zone_map=csi_zones,
                noise_diff_threshold=noise_diff,
                score_norm_max=score_norm,
                transition_window=trans_window,
            )

            self.engines[name] = FusionEngine(
                threshold=pair.get("confidence_threshold", 0.7),
                max_latency_ms=pair.get("max_latency_ms", 2000),
                debounce_in_s=pair.get("debounce_in_s", 1.0),
                clearing_s=pair.get("clearing_s", 5.0),
                ld2450_solo_penalty=pair.get("ld2450_solo_penalty", 0.0),
                min_present_s=pair.get("min_present_s", 30.0),
                exit_threshold=pair.get("exit_threshold", 0.25),
            )
            self.last_results[name] = FusionResult()

        # Telegram notifier
        tg_cfg = self.config.get("telegram", {})
        tg_token = os.environ.get("TELEGRAM_TOKEN") or tg_cfg.get("token", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or tg_cfg.get("chat_id", "")
        self.telegram = TelegramNotifier(
            token=tg_token,
            chat_id=tg_chat,
            cooldown_s=tg_cfg.get("cooldown_s", 30.0),
        )

        # Tapo camera PTZ controller
        tapo_cfg = self.config.get("tapo_cameras", [])
        self.tapo = TapoCameraController(tapo_cfg)

        # MQTT client
        mqtt_cfg = self.config["mqtt"]
        self.client = mqtt.Client(client_id="fusion-service", clean_session=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        user = mqtt_cfg.get("user", "")
        password = os.environ.get("MQTT_PASSWORD") or mqtt_cfg.get("password", "")
        if user:
            self.client.username_pw_set(user, password)

        self.client.will_set("security/fusion/availability", "offline", qos=1, retain=True)

        self.mqtt_host = mqtt_cfg["host"]
        self.mqtt_port = mqtt_cfg.get("port", 1883)

        self._mqtt_connected_once = False

        # Calibration persistence
        cal_cfg = self.config.get("calibration", {})
        cal_path = cal_cfg.get("path", "calibration/thresholds.json")
        if not os.path.isabs(cal_path):
            cal_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), cal_path)
        self._calibration_path = cal_path
        self._load_calibration()

        # SQLite database
        db_cfg = self.config.get("database", {})
        db_path = db_cfg.get("path", "data/fusion.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), db_path)
        self.db = FusionDatabase(
            db_path=db_path,
            max_size_mb=db_cfg.get("max_size_mb", 500),
        )

        # Hourly stats accumulator: csi_id → {variance_sum, turbulence_sum, amp_sum, motion_count, total_count}
        self._hourly_accum: dict[str, dict] = {}
        self._last_hourly_flush: float = time.time()

        # Auto-discovery config
        disc_cfg = self.config.get("auto_discovery", {})
        self._discovery_enabled = disc_cfg.get("enabled", False)
        self._discovery_default_pair = disc_cfg.get("default_pair", "")
        self._discovery_csi_prefix = disc_cfg.get("csi_prefix", "csi_")

        # HA ground truth poller (optional)
        self.ha_poller: Optional[HAGroundTruthPoller] = None
        ha_cfg = self.config.get("home_assistant")
        if ha_cfg:
            try:
                self.ha_poller = HAGroundTruthPoller(
                    ha_config=ha_cfg, db=self.db,
                    csi_states=self.csi_states,
                    last_results=self.last_results,
                )
            except Exception as e:
                log.warning("HA ground truth disabled: %s", e)

        # Register HA sensors as active fusion inputs per pair
        # Weights tuned from logistic regression on 470k snapshots (2026-04-03):
        # - MW occupancy is high-reliability ground truth
        # - PIR weight reduced: data showed same trigger rate for occupied/empty in obyvak
        _weight_map = {
            "mw_occupancy": 0.40,
            "pir_motion": 0.10,
            "camera_motion": 0.20,
            "camera_person": 0.35,
        }
        for pair in self.config["pairs"]:
            pair_name = pair["name"]
            self._ha_sensor_map[pair_name] = []
            for hs_cfg in pair.get("ha_sensors", []):
                eid = hs_cfg["entity_id"]
                stype = hs_cfg.get("type", "mw_occupancy")
                weight = hs_cfg.get("weight", _weight_map.get(stype, 0.15))
                event_window = hs_cfg.get("event_window_s", 120.0)
                stuck_timeout = hs_cfg.get("stuck_timeout_s", 600.0)
                warmup = hs_cfg.get("warmup_s", 0.0)
                sensor = HASensorInput(
                    entity_id=eid, sensor_type=stype, weight=weight,
                    event_window_s=event_window, stuck_timeout_s=stuck_timeout,
                    warmup_s=warmup)
                self._ha_sensor_map[pair_name].append(eid)
                if self.ha_poller:
                    self.ha_poller.active_states[eid] = sensor
            if self._ha_sensor_map[pair_name]:
                log.info("HA fusion sensors for '%s': %d entities",
                         pair_name, len(self._ha_sensor_map[pair_name]))

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path, "r") as f:
            return yaml.safe_load(f)

    def _save_calibration(self):
        """Persist per-node calibration thresholds to JSON."""
        try:
            os.makedirs(os.path.dirname(self._calibration_path), exist_ok=True)
            data = {csi_id: cal.to_dict() for csi_id, cal in self.csi_calibration.items()}
            tmp = self._calibration_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._calibration_path)
            log.info("Calibration saved to %s (%d nodes)", self._calibration_path, len(data))
        except Exception as e:
            log.warning("Failed to save calibration: %s", e)

    def _load_calibration(self):
        """Load persisted calibration thresholds on startup."""
        if not os.path.exists(self._calibration_path):
            log.info("No calibration file at %s, starting fresh", self._calibration_path)
            return
        try:
            with open(self._calibration_path, "r") as f:
                data = json.load(f)
            for csi_id, cal_data in data.items():
                model = CSICalibrationModel.from_dict(cal_data)
                self.csi_calibration[csi_id] = model
                log.info("Loaded calibration for %s: threshold=%.4f (idle=%d, motion=%d)",
                         csi_id, model.current_threshold, model.idle_n, model.motion_n)
        except Exception as e:
            log.warning("Failed to load calibration from %s: %s", self._calibration_path, e)

    # -- MQTT callbacks --

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed: rc=%d", rc)
            return

        log.info("MQTT connected to %s:%d", self.mqtt_host, self.mqtt_port)
        client.publish("security/fusion/availability", "online", qos=1, retain=True)

        for pair in self.config["pairs"]:
            ld2412 = pair.get("ld2412_id", "")
            ld2450 = pair.get("ld2450_id", "")

            # LD2412 topics
            if ld2412:
                for sub in ("presence/state", "presence/distance", "presence/energy_mov",
                            "presence/energy_stat", "presence/direction", "alarm/state", "radar_type"):
                    client.subscribe(f"security/{ld2412}/{sub}")

            # LD2450 topics
            if ld2450:
                for sub in ("presence/state", "tracking/count", "alarm/state",
                            "radar_type", "availability"):
                    client.subscribe(f"security/{ld2450}/{sub}")
                for i in range(1, 4):
                    client.subscribe(f"security/{ld2450}/tracking/target{i}/x")
                    client.subscribe(f"security/{ld2450}/tracking/target{i}/y")
                    client.subscribe(f"security/{ld2450}/tracking/target{i}/s")

            # CSI topics (ESPectre via direct MQTT)
            for csi_id in pair.get("csi_ids", []):
                # ESPHome MQTT uses: {topic_prefix}/binary_sensor/{object_id}/state
                # Wildcard + matches any object_id
                client.subscribe(f"esphome/{csi_id}/binary_sensor/+/state")
                client.subscribe(f"esphome/{csi_id}/sensor/+/state")
                client.subscribe(f"esphome/{csi_id}/text_sensor/+/state")

        # Auto-discovery subscription
        if self._discovery_enabled:
            client.subscribe("esphome/discover/#")
            log.info("Auto-discovery enabled, subscribed to esphome/discover/#")

        log.info("Subscribed to %d sensor pair(s)", len(self.config["pairs"]))
        self._publish_ha_discovery()

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if not payload:
            return

        parts = topic.split("/")

        # Handle security/{device_id}/... topics
        if len(parts) >= 3 and parts[0] == "security":
            device_id = parts[1]
            sub_topic = "/".join(parts[2:])

            if device_id in self.ld2412_states:
                st = self.ld2412_states[device_id]
                st.last_update = time.time()
                self._update_ld2412(st, sub_topic, payload)
                self._run_fusion_for_device(device_id, "ld2412")

            if device_id in self.ld2450_states:
                st = self.ld2450_states[device_id]
                st.last_update = time.time()
                self._update_ld2450(st, sub_topic, payload)
                self._run_fusion_for_device(device_id, "ld2450")

            if device_id in self.csi_states:
                st = self.csi_states[device_id]
                st.last_update = time.time()
                self._update_csi(st, sub_topic, payload)
                self._run_fusion_for_device(device_id, "csi")

        # Handle esphome/discover/{name} (auto-discovery)
        elif (len(parts) >= 3 and parts[0] == "esphome" and parts[1] == "discover"
              and self._discovery_enabled):
            self._handle_discovery(parts[2], payload)

        # Handle esphome/{device_id}/... topics (ESPHome default)
        elif len(parts) >= 4 and parts[0] == "esphome":
            device_id = parts[1]
            if device_id in self.csi_states:
                st = self.csi_states[device_id]
                st.last_update = time.time()
                esphome_sub = "/".join(parts[2:])
                self._update_csi_esphome(st, esphome_sub, payload)
                self._run_fusion_for_device(device_id, "csi")

    @staticmethod
    def _update_ld2412(st: LD2412State, sub: str, val: str):
        if sub == "presence/state":
            st.presence = val.lower() in ("detected", "1", "true", "on")
        elif sub == "presence/distance":
            st.distance_cm = int(val) if val.lstrip("-").isdigit() else 0
        elif sub == "presence/energy_mov":
            st.moving_energy = int(val) if val.isdigit() else 0
        elif sub == "presence/energy_stat":
            st.static_energy = int(val) if val.isdigit() else 0
        elif sub == "presence/direction":
            st.direction = val
        elif sub == "alarm/state":
            st.alarm_state = val

    def _update_ld2450(self, st: LD2450State, sub: str, val: str):
        if sub == "presence/state":
            st.presence = val.lower() in ("detected", "1", "true", "on")
        elif sub == "tracking/count":
            st.target_count = int(val) if val.isdigit() else 0
        elif sub == "alarm/state":
            st.alarm_state = val
        elif sub == "availability":
            if val.lower() == "offline":
                log.warning("LD2450 %s went OFFLINE", st.device_id)
            else:
                log.info("LD2450 %s is ONLINE", st.device_id)
        elif sub.startswith("tracking/target"):
            try:
                idx = int(sub.split("/")[1].replace("target", "")) - 1
                coord = sub.split("/")[2]
                v = int(val)
                if 0 <= idx < 3:
                    # -9999 = invalid/cleared target from firmware
                    if v == -9999:
                        st.targets[idx].x = 0
                        st.targets[idx].y = 0
                        st.targets[idx].last_update = 0
                        return
                    if coord == "x":
                        st.targets[idx].x = v
                    elif coord == "y":
                        st.targets[idx].y = v
                    elif coord == "s":
                        st.targets[idx].speed = v
                    st.targets[idx].last_update = time.time()
            except (ValueError, IndexError):
                pass

    @staticmethod
    def _update_csi(st: CSIState, sub: str, val: str):
        if sub == "motion":
            st.motion = val.lower() in ("on", "1", "true", "motion", "detected")
        elif sub == "movement_score":
            try:
                st.movement_score = float(val)
            except ValueError:
                pass

    @staticmethod
    def _update_csi_esphome(st: CSIState, sub: str, val: str):
        if "binary_sensor" in sub and "motion" in sub:
            st.motion = val.lower() in ("on", "1", "true")
        elif "binary_sensor" in sub and "calibrating" in sub:
            st.calibrating = val.lower() in ("on", "1", "true")
        elif "binary_sensor" in sub and "detector_ready" in sub:
            st.detector_ready = val.lower() in ("on", "1", "true")
        elif "sensor" in sub and "movement" in sub:
            try:
                st.movement_score = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_turbulence" in sub and "mean" not in sub:
            try:
                st.turbulence = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_variance" in sub:
            try:
                st.variance = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_mean_turbulence" in sub:
            try:
                st.mean_turbulence = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_active_threshold" in sub:
            try:
                st.active_threshold = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_packets" in sub:
            try:
                st.packets_total = int(float(val))
            except ValueError:
                pass
        elif "sensor" in sub and "csi_amplitude_sum" in sub:
            try:
                st.amplitude_sum = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_phase_turbulence" in sub:
            try:
                st.phase_turbulence = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_idle_mean_turbulence" in sub:
            try:
                st.idle_mean_turbulence = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_ratio_turbulence" in sub:
            try:
                st.ratio_turbulence = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_composite_score" in sub:
            try:
                st.composite_score = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_amplitude_deviation" in sub:
            try:
                st.amplitude_deviation = float(val)
            except ValueError:
                pass
        elif "sensor" in sub and "csi_breathing_score" in sub:
            try:
                st.breathing_score = float(val)
            except ValueError:
                pass
        elif "text_sensor" in sub and "subcarrier_amplitudes" in sub:
            try:
                st.subcarrier_amplitudes = [float(v) for v in val.split(",") if v]
            except ValueError:
                pass

    def _handle_discovery(self, node_name: str, payload: str):
        """Handle esphome/discover/{name} message for ATOM auto-discovery."""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            data = {}

        ip = data.get("ip", "unknown")
        friendly = data.get("friendly_name", node_name)
        node_id = node_name

        # Derive zone from node_id (csi_obyvak → obyvak)
        zone = ""
        if node_id.startswith(self._discovery_csi_prefix):
            zone = node_id[len(self._discovery_csi_prefix):]

        is_new = self.db.log_discovered_node(node_id, ip, zone)
        if not is_new:
            return

        log.info("Discovered new node: %s (%s) zone=%s", node_id, ip, zone)

        # Auto-add CSI nodes to default pair if configured
        if (node_id.startswith(self._discovery_csi_prefix) and
                self._discovery_default_pair):
            # Check if not already in any pair's csi_ids
            already_tracked = any(
                node_id in p.get("csi_ids", []) for p in self.config["pairs"])
            if not already_tracked:
                # Add to runtime state (won't persist to config file)
                for pair in self.config["pairs"]:
                    if pair["name"] == self._discovery_default_pair:
                        pair.setdefault("csi_ids", []).append(node_id)
                        csi_zones = pair.setdefault("csi_zones", {})
                        csi_zones[node_id] = zone or node_id
                        self.csi_states[node_id] = CSIState(device_id=node_id)
                        # Subscribe to new node's topics
                        self.client.subscribe(f"esphome/{node_id}/binary_sensor/+/state")
                        self.client.subscribe(f"esphome/{node_id}/sensor/+/state")
                        self.client.subscribe(f"esphome/{node_id}/text_sensor/+/state")
                        self.db.log_discovered_node(node_id, ip, zone, auto_added=True)
                        log.info("Auto-added %s to pair '%s'",
                                 node_id, self._discovery_default_pair)
                        break

        self.telegram.send(
            f"🆕 <b>Novy CSI node</b>\n"
            f"{friendly} ({ip})\nZone: {zone or 'N/A'}",
            force=True,
        )

    def _accumulate_csi_stats(self, csi_id: str, csi_st: CSIState):
        """Accumulate CSI stats for hourly flush."""
        acc = self._hourly_accum.setdefault(csi_id, {
            "variance_sum": 0.0, "turbulence_sum": 0.0, "amp_sum": 0.0,
            "phase_turb_sum": 0.0, "ratio_turb_sum": 0.0,
            "breathing_sum": 0.0, "motion_count": 0, "total_count": 0,
        })
        acc["variance_sum"] += csi_st.variance
        acc["turbulence_sum"] += csi_st.turbulence
        acc["amp_sum"] += csi_st.amplitude_sum
        acc["phase_turb_sum"] += csi_st.phase_turbulence
        acc["ratio_turb_sum"] += csi_st.ratio_turbulence
        acc["breathing_sum"] += csi_st.breathing_score
        if csi_st.motion:
            acc["motion_count"] += 1
        acc["total_count"] += 1

    def _write_periodic_snapshots(self):
        """Timer-based snapshot writer (každých 10s per pair, nezávisle na MQTT)."""
        now = time.time()
        if not hasattr(self, '_last_snapshot_time'):
            self._last_snapshot_time = {}
        if not hasattr(self, '_last_successful_snapshot'):
            self._last_successful_snapshot = {}

        for pair in self.config["pairs"]:
            name = pair["name"]
            if now - self._last_snapshot_time.get(name, 0.0) < 10.0:
                continue
            self._last_snapshot_time[name] = now

            ld2412_id = pair.get("ld2412_id", "")
            ld2450_id = pair.get("ld2450_id", "")
            ld2412 = self.ld2412_states.get(ld2412_id) if ld2412_id else None
            ld2450 = self.ld2450_states.get(ld2450_id) if ld2450_id else None
            pair_csi_states = {cid: self.csi_states[cid]
                               for cid in pair.get("csi_ids", [])
                               if cid in self.csi_states}
            ha_inputs = []
            if self.ha_poller:
                for eid in self._ha_sensor_map.get(name, []):
                    hs = self.ha_poller.active_states.get(eid)
                    if hs:
                        ha_inputs.append(hs)
            result = self.last_results.get(name)

            try:
                self.db.log_snapshot(name, result, ld2450, ld2412, pair_csi_states, ha_inputs)
                self._last_successful_snapshot[name] = now
            except Exception as exc:
                log.error("Snapshot write failed [%s]: %s", name, exc)

    def _check_snapshot_health(self):
        """Watchdog: pokud snapshot přestal zapisovat >5 min, pošle Telegram (max 1×/hod)."""
        now = time.time()
        if not hasattr(self, '_last_snapshot_health_check'):
            self._last_snapshot_health_check = now
            return
        if now - self._last_snapshot_health_check < 1800:
            return
        self._last_snapshot_health_check = now

        if not hasattr(self, '_last_successful_snapshot'):
            return

        stale = []
        for pair in self.config["pairs"]:
            name = pair["name"]
            last_ok = self._last_successful_snapshot.get(name, 0.0)
            if last_ok > 0 and now - last_ok > 1800:
                stale.append(f"{name}: {int((now - last_ok) / 60)}m bez snapshotu")

        if stale:
            if not hasattr(self, '_last_snapshot_alert') or now - self._last_snapshot_alert > 3600:
                self._last_snapshot_alert = now
                log.error("Snapshot watchdog: %s", ", ".join(stale))
                self.telegram.send(
                    "⚠️ <b>Fusion: snapshot výpadek</b>\n" + "\n".join(stale),
                    force=True)

    def _flush_hourly_stats(self):
        """Flush accumulated CSI stats to DB and run cleanup."""
        now = time.time()
        if (now - self._last_hourly_flush) < 3600:
            return
        self._last_hourly_flush = now

        for csi_id, acc in self._hourly_accum.items():
            n = acc["total_count"]
            if n == 0:
                continue
            csi_st = self.csi_states.get(csi_id)
            packets = csi_st.packets_total if csi_st else 0
            self.db.log_csi_stats(
                node_id=csi_id,
                avg_variance=acc["variance_sum"] / n,
                avg_turbulence=acc["turbulence_sum"] / n,
                avg_amplitude=acc["amp_sum"] / n,
                motion_pct=100.0 * acc["motion_count"] / n,
                packet_count=packets,
                avg_phase_turbulence=acc["phase_turb_sum"] / n,
                avg_ratio_turbulence=acc["ratio_turb_sum"] / n,
                avg_breathing_score=acc["breathing_sum"] / n,
            )

        self._hourly_accum.clear()

        # Flush per-subcarrier stats for all active analyzers
        for node_id, analyzer in self.subcarrier_analyzers.items():
            self.db.flush_subcarrier_stats(node_id, analyzer)
        if self.subcarrier_analyzers:
            log.info("Subcarrier stats flushed for %d nodes", len(self.subcarrier_analyzers))

        # Sensor connectivity monitoring (per-device staleness check)
        # optional_sensors: true → only log, no Telegram (dynamic ESP setup)
        offline_sensors = []
        alert_worthy = []  # only non-optional pairs
        for pair in self.config["pairs"]:
            name = pair["name"]
            optional = pair.get("optional_sensors", False)
            pair_offline = []
            # Check LD2412
            ld2412_id = pair.get("ld2412_id", "")
            if ld2412_id:
                st = self.ld2412_states.get(ld2412_id)
                if st and (now - st.last_update) > 300:
                    pair_offline.append(f"{ld2412_id}: {int((now - st.last_update)/60)}m")
            # Check LD2450
            ld2450_id = pair.get("ld2450_id", "")
            if ld2450_id:
                st = self.ld2450_states.get(ld2450_id)
                if st and (now - st.last_update) > 300:
                    pair_offline.append(f"{ld2450_id}: {int((now - st.last_update)/60)}m")
            # Check CSI nodes
            for csi_id in pair.get("csi_ids", []):
                st = self.csi_states.get(csi_id)
                if st and (now - st.last_update) > 300:
                    pair_offline.append(f"{csi_id}: {int((now - st.last_update)/60)}m")

            if pair_offline:
                offline_sensors.extend(f"{s} ({name})" for s in pair_offline)
                if not optional:
                    alert_worthy.extend(f"{s} ({name})" for s in pair_offline)

        if offline_sensors:
            log.info("Offline sensors: %s", "; ".join(offline_sensors))
        if alert_worthy:
            if not hasattr(self, '_last_offline_alert') or now - self._last_offline_alert > 3600:
                self._last_offline_alert = now
                self.telegram.send(
                    "<b>Offline senzory</b>\n" + "\n".join(alert_worthy),
                    force=True)

        try:
            self.db.cleanup_if_needed()
        except Exception as exc:
            log.error("DB cleanup failed: %s", exc)
        log.info("Hourly CSI stats flushed to DB")

    def _run_fusion_for_device(self, device_id: str, sensor_type: str):
        """Find the pair containing this device_id and run fusion."""
        for pair in self.config["pairs"]:
            match = False
            if sensor_type == "ld2412" and pair.get("ld2412_id") == device_id:
                match = True
            elif sensor_type == "ld2450" and pair.get("ld2450_id") == device_id:
                match = True
            elif sensor_type == "csi" and device_id in pair.get("csi_ids", []):
                match = True

            if not match:
                continue

            name = pair["name"]
            engine = self.engines[name]
            ld2412_id = pair.get("ld2412_id", "")
            ld2450_id = pair.get("ld2450_id", "")
            ld2412 = self.ld2412_states.get(ld2412_id) if ld2412_id else None
            ld2450 = self.ld2450_states.get(ld2450_id) if ld2450_id else None

            # Gather CSI states for this pair
            csi_ids = pair.get("csi_ids", [])
            pair_csi_states = {}
            csi = None
            for csi_id in csi_ids:
                c = self.csi_states.get(csi_id)
                if c:
                    pair_csi_states[csi_id] = c
                    if csi is None or c.last_update > csi.last_update:
                        csi = c

            # Dual-CSI analysis
            now = time.time()
            analyzer = self.dual_csi_analyzers.get(name)
            dual_csi = None
            if analyzer and len(csi_ids) >= 2:
                dual_csi = analyzer.analyze(pair_csi_states, now)

            # Amplitude-based static presence detection
            amp_blocked = False
            for csi_id in csi_ids:
                csi_st = self.csi_states.get(csi_id)
                if not csi_st or csi_st.calibrating or not csi_st.detector_ready:
                    continue
                tracker = self.amplitude_trackers.setdefault(
                    csi_id, AmplitudeBaselineTracker())
                cal = self.csi_calibration.get(csi_id)
                thr = cal.current_threshold if cal else 0.025
                if tracker.update(csi_st.amplitude_sum, csi_st.variance, thr, now):
                    amp_blocked = True

            # Gather HA sensor inputs for this pair
            ha_inputs = []
            if self.ha_poller:
                for eid in self._ha_sensor_map.get(name, []):
                    hs = self.ha_poller.active_states.get(eid)
                    if hs:
                        ha_inputs.append(hs)

            result = engine.fuse(ld2412, ld2450, csi, dual_csi,
                                 amplitude_blocked=amp_blocked,
                                 ha_sensors=ha_inputs or None)

            # --- Temporal CSI continuity bonus ---
            if "csi" in result.source:
                cnt = self._csi_continuity_count.get(name, 0) + 1
                self._csi_continuity_count[name] = cnt
                if cnt >= 3:
                    result.confidence = min(result.confidence + 0.05, 1.0)
                    result.reason += "; continuity_bonus"
            else:
                self._csi_continuity_count[name] = 0

            # --- Adaptive CSI threshold (self-calibrating) ---
            uptime = now - self._startup_time
            for csi_id in csi_ids:
                csi_st = self.csi_states.get(csi_id)
                if not csi_st or csi_st.calibrating or not csi_st.detector_ready:
                    continue
                model = self.csi_calibration.setdefault(
                    csi_id, CSICalibrationModel(csi_id))
                # Lock-in: skip recording during first 5 min (unstable startup)
                if uptime >= 300:
                    # Only feed idle model when FSM is CLEAR (P6: prevents
                    # absorbing motion variance into idle baseline)
                    fsm_idle = engine.state_machine.state == FusionState.CLEAR
                    model.record(csi_st.variance, is_idle=fsm_idle)

                last_t = self._adaptive_threshold_last.get(csi_id, 0.0)
                # Lock-in: skip recalculation during first 15 min
                if (now - last_t) >= 30.0 and uptime >= 900:
                    new_thr = model.recalculate()
                    if new_thr is not None:
                        self.client.publish(
                            f"esphome/{csi_id}/number/threshold/command",
                            f"{new_thr:.4f}")
                        self._adaptive_threshold_last[csi_id] = now
                        self._save_calibration()
                        # Log calibration to DB
                        sorted_v = sorted(model.variances)
                        n_v = len(sorted_v)
                        p80 = sorted_v[int(n_v * 0.80)] if n_v > 0 else 0
                        p90 = sorted_v[int(n_v * 0.90)] if n_v > 0 else 0
                        self.db.log_calibration(
                            csi_id, new_thr, model.idle_n, model.motion_n, p80, p90)
                        log.info("CSI auto-threshold %s: %.4f "
                                 "(samples=%d, idle=%d, motion=%d)",
                                 csi_id, new_thr,
                                 len(model.variances),
                                 model.idle_n, model.motion_n)

            # --- Adaptive filter parameters (packet rate monitor) ---
            for csi_id in csi_ids:
                csi_st = self.csi_states.get(csi_id)
                if not csi_st or csi_st.packets_total <= 0:
                    continue
                prev_rate = self._packet_rate_last.get(csi_id)
                if prev_rate is None:
                    self._packet_rate_last[csi_id] = (now, csi_st.packets_total)
                    continue
                prev_time, prev_pkts = prev_rate
                dt = now - prev_time
                if dt < 10.0:
                    continue
                rate = (csi_st.packets_total - prev_pkts) / dt
                self._packet_rate_last[csi_id] = (now, csi_st.packets_total)
                if rate <= 0:
                    continue
                # If rate deviates >10% from 100Hz, adjust lowpass cutoff
                if abs(rate - 100.0) / 100.0 > 0.10:
                    new_cutoff = 11.0 * rate / 100.0
                    new_cutoff = max(5.0, min(20.0, new_cutoff))
                    last_adj = self._filter_adjust_last.get(csi_id, 0.0)
                    if (now - last_adj) >= 60.0:
                        self.client.publish(
                            f"esphome/{csi_id}/filter/lowpass_cutoff/command",
                            f"{new_cutoff:.1f}")
                        self._filter_adjust_last[csi_id] = now
                        log.info("Adaptive filter %s: rate=%.0fHz → cutoff=%.1f",
                                 csi_id, rate, new_cutoff)

            # --- Subcarrier analysis + recalibration trigger ---
            for csi_id in csi_ids:
                csi_st = self.csi_states.get(csi_id)
                if not csi_st or not csi_st.subcarrier_amplitudes:
                    continue
                sc_analyzer = self.subcarrier_analyzers.setdefault(
                    csi_id, SubcarrierAnalyzer())
                sc_analyzer.update(csi_st.subcarrier_amplitudes)
                if sc_analyzer.check_shift(csi_st.subcarrier_amplitudes, now):
                    self.client.publish(
                        f"esphome/{csi_id}/command/recalibrate", "1")
                    log.warning("Subcarrier shift detected on %s → recalibrate",
                                csi_id)

            # --- Accumulate CSI stats for hourly flush ---
            for csi_id in csi_ids:
                csi_st = self.csi_states.get(csi_id)
                if csi_st and csi_st.detector_ready:
                    self._accumulate_csi_stats(csi_id, csi_st)

            prev = self.last_results[name]
            # Rate-limit MQTT publish to reduce HA SD card writes
            # Critical: only presence flip or alarm change (instant publish)
            # Changed: everything else (rate-limited to max 1x per 5s)
            last_pub = getattr(self, '_last_publish_time', {}).get(name, 0.0)
            critical = (result.presence != prev.presence or
                        result.alarm_state != prev.alarm_state)
            changed = (result.state != prev.state or
                       result.source != prev.source or
                       abs(result.confidence - prev.confidence) >= 0.15 or
                       result.active_zone != prev.active_zone or
                       result.movement_direction != prev.movement_direction)

            # Always log events on state change (independent of MQTT rate-limit)
            if critical or result.state != prev.state:
                event_type = "presence" if result.presence else "clear"
                try:
                    self.db.log_event(
                        pair=name, event_type=event_type, state="detected" if result.presence else "clear",
                        confidence=result.confidence, source=result.source,
                        zone=result.active_zone, distance_mm=result.distance_mm,
                        fsm_state=result.state.value,
                        details={"reason": result.reason, "direction": result.movement_direction},
                    )
                except Exception as e:
                    log.warning("Failed to log event for %s: %s", name, e)

            if critical or (changed and (now - last_pub) >= 5.0):
                self._publish_fusion(name, result, csi, ld2412, ld2450)
                self.last_results[name] = result
                if not hasattr(self, '_last_publish_time'):
                    self._last_publish_time = {}
                self._last_publish_time[name] = now


    def _publish_fusion(self, pair_name: str, result: FusionResult,
                        csi: Optional[CSIState] = None,
                        ld2412: Optional[LD2412State] = None,
                        ld2450: Optional[LD2450State] = None):
        base = f"security/fusion/{pair_name}"
        state_str = "detected" if result.presence else "clear"

        # Event logging moved to _run_fusion_for_device (independent of MQTT rate-limit)

        self.client.publish(f"{base}/state", state_str, retain=True)
        self.client.publish(f"{base}/confidence", str(result.confidence), retain=True)
        self.client.publish(f"{base}/source", result.source, retain=True)

        # Tapo PTZ — trigger on presence/zone change
        self.tapo.on_fusion_result(pair_name, result)
        self.client.publish(f"{base}/alarm_state", result.alarm_state, retain=True)
        self.client.publish(f"{base}/fsm_state", result.state.value, retain=True)
        self.client.publish(f"{base}/active_zone", result.active_zone, retain=True)
        self.client.publish(f"{base}/movement_direction", result.movement_direction, retain=True)

        attrs = {
            "confidence": result.confidence,
            "source": result.source,
            "distance_mm": result.distance_mm,
            "alarm_state": result.alarm_state,
            "fsm_state": result.state.value,
            "reason": result.reason,
            "active_zone": result.active_zone,
            "movement_direction": result.movement_direction,
        }
        if csi is not None:
            if csi.mean_turbulence > 0.001:
                anomaly_ratio = csi.turbulence / csi.mean_turbulence
            else:
                anomaly_ratio = csi.movement_score / max(csi.active_threshold, 0.001)
            attrs["csi_anomaly_ratio"] = round(anomaly_ratio, 2)
            attrs["csi_turbulence"] = round(csi.turbulence, 4)
            attrs["csi_mean_turbulence"] = round(csi.mean_turbulence, 4)
            attrs["csi_variance"] = round(csi.variance, 4)
            attrs["csi_amplitude_sum"] = round(csi.amplitude_sum, 2)
            attrs["csi_threshold"] = round(csi.active_threshold, 4)
            attrs["csi_packets"] = csi.packets_total
            attrs["csi_calibrating"] = csi.calibrating
        # Amplitude baseline info
        for csi_id, tracker in self.amplitude_trackers.items():
            short = csi_id.replace("csi_", "")
            attrs[f"csi_amp_p20_{short}"] = round(tracker.baseline_p20, 2)
        # Per-node calibration info
        for csi_id, cal in self.csi_calibration.items():
            short = csi_id.replace("csi_", "")
            attrs[f"csi_auto_thr_{short}"] = round(cal.current_threshold, 4)
            attrs[f"csi_samples_{short}"] = len(cal.variances)
            attrs[f"csi_idle_n_{short}"] = cal.idle_n
            attrs[f"csi_motion_n_{short}"] = cal.motion_n
        self.client.publish(f"{base}/attributes", json.dumps(attrs), retain=True)

        log.info(
            "FUSION [%s] %s  conf=%.2f  src=%-15s  fsm=%-10s  zone=%-10s  dist=%.0fmm  | %s",
            pair_name,
            "DETECTED" if result.presence else "CLEAR   ",
            result.confidence,
            result.source,
            result.state.value,
            result.active_zone,
            result.distance_mm,
            result.reason,
        )

        # --- Telegram notifications ---
        # Presence/zone alerts only when armed; alarm state changes always
        armed = result.alarm_state != "disarmed"
        prev = self.last_results.get(pair_name)
        if prev is not None:
            # Alarm state change (always notify)
            if result.alarm_state != prev.alarm_state and result.alarm_state != "disarmed":
                self.telegram.send(
                    f"🚨 <b>ALARM: {result.alarm_state.upper()}</b>\n"
                    f"Conf: {result.confidence:.0%} | {result.source}",
                    force=True,
                )

            if armed:
                # State change: CLEAR↔DETECTED
                if result.presence and not prev.presence:
                    zone_info = f" [{result.active_zone}]" if result.active_zone != "none" else ""
                    lines = [f"🔴 <b>DETECTED</b>{zone_info}"]
                    lines.append(f"Conf: {result.confidence:.0%} | {result.source}")
                    if ld2412 and ld2412.presence:
                        lines.append(f"LD2412: {ld2412.distance_cm}cm, {ld2412.direction}")
                    if ld2450:
                        targets = [t for t in ld2450.targets if t.x != 0 or t.y != 0]
                        if targets:
                            t_info = ", ".join(f"{t.distance_mm/1000:.1f}m" for t in targets)
                            lines.append(f"LD2450: {len(targets)} cíl(e) [{t_info}]")
                    if csi and "csi" in result.source:
                        lines.append(f"CSI: var={csi.variance:.4f} thr={csi.active_threshold:.4f}")
                        if result.active_zone != "none":
                            lines.append(f"Zone: {result.active_zone}")
                    self.telegram.send("\n".join(lines))
                elif not result.presence and prev.presence:
                    self.telegram.send(f"🟢 <b>CLEAR</b> [{pair_name}]")

                # Zone transition
                if (result.movement_direction != "none" and
                        result.movement_direction != prev.movement_direction):
                    self.telegram.send(
                        f"🚶 <b>{result.movement_direction}</b>\n"
                        f"Conf: {result.confidence:.0%}"
                )

    @staticmethod
    def _model_string(pair: dict) -> str:
        parts = []
        if pair.get("ld2412_id"):
            parts.append("LD2412")
        if pair.get("ld2450_id"):
            parts.append("LD2450")
        if pair.get("csi_ids"):
            parts.append("CSI")
        return "+".join(parts) or "CSI-only"

    def _publish_ha_discovery(self):
        """Publish Home Assistant MQTT auto-discovery configs."""
        for pair in self.config["pairs"]:
            name = pair["name"]
            base = f"security/fusion/{name}"
            has_csi = bool(pair.get("csi_ids"))

            device = {
                "ids": f"radar_fusion_{name}",
                "name": f"Radar Fusion - {name.capitalize()}",
                "mdl": self._model_string(pair),
                "mf": "DIY",
            }

            # Binary sensor: fused presence
            self.client.publish(
                f"homeassistant/binary_sensor/fusion_{name}_presence/config",
                json.dumps({
                    "name": f"{name.capitalize()} Presence (Fused)",
                    "uniq_id": f"fusion_{name}_presence",
                    "stat_t": f"{base}/state",
                    "dev_cla": "occupancy",
                    "pl_on": "detected",
                    "pl_off": "clear",
                    "json_attr_t": f"{base}/attributes",
                    "avty_t": "security/fusion/availability",
                    "dev": device,
                }), retain=True,
            )

            # Sensor: confidence
            self.client.publish(
                f"homeassistant/sensor/fusion_{name}_confidence/config",
                json.dumps({
                    "name": f"{name.capitalize()} Fusion Confidence",
                    "uniq_id": f"fusion_{name}_confidence",
                    "stat_t": f"{base}/confidence",
                    "ic": "mdi:signal-variant",
                    "avty_t": "security/fusion/availability",
                    "dev": device,
                }), retain=True,
            )

            # Sensor: source
            self.client.publish(
                f"homeassistant/sensor/fusion_{name}_source/config",
                json.dumps({
                    "name": f"{name.capitalize()} Fusion Source",
                    "uniq_id": f"fusion_{name}_source",
                    "stat_t": f"{base}/source",
                    "ic": "mdi:radar",
                    "avty_t": "security/fusion/availability",
                    "dev": device,
                }), retain=True,
            )

            # Sensor: FSM state
            self.client.publish(
                f"homeassistant/sensor/fusion_{name}_fsm/config",
                json.dumps({
                    "name": f"{name.capitalize()} Detection State",
                    "uniq_id": f"fusion_{name}_fsm",
                    "stat_t": f"{base}/fsm_state",
                    "ic": "mdi:state-machine",
                    "avty_t": "security/fusion/availability",
                    "dev": device,
                }), retain=True,
            )

            # Sensor: active zone (dual-CSI)
            if len(pair.get("csi_ids", [])) >= 2:
                self.client.publish(
                    f"homeassistant/sensor/fusion_{name}_zone/config",
                    json.dumps({
                        "name": f"{name.capitalize()} Active Zone",
                        "uniq_id": f"fusion_{name}_zone",
                        "stat_t": f"{base}/active_zone",
                        "ic": "mdi:map-marker",
                        "avty_t": "security/fusion/availability",
                        "dev": device,
                    }), retain=True,
                )

                # Sensor: movement direction (dual-CSI)
                self.client.publish(
                    f"homeassistant/sensor/fusion_{name}_direction/config",
                    json.dumps({
                        "name": f"{name.capitalize()} Movement Direction",
                        "uniq_id": f"fusion_{name}_direction",
                        "stat_t": f"{base}/movement_direction",
                        "ic": "mdi:arrow-left-right",
                        "avty_t": "security/fusion/availability",
                        "dev": device,
                    }), retain=True,
                )

        log.info("HA discovery published for %d pair(s)", len(self.config["pairs"]))

        # --- Lifecycle Telegram ---
        if not self._mqtt_connected_once:
            self._mqtt_connected_once = True
            n_pairs = len(self.config["pairs"])
            csi_ids_all = set()
            for p in self.config["pairs"]:
                csi_ids_all.update(p.get("csi_ids", []))
            n_csi = len(csi_ids_all)
            n_ld2412 = len(self.ld2412_states)
            n_ld2450 = len(self.ld2450_states)
            cal_loaded = len(self.csi_calibration)
            cal_info = (f"loaded {cal_loaded} node(s)"
                        if cal_loaded > 0 else "fresh")
            self.telegram.send(
                f"\U0001f7e2 <b>Fusion service started</b>\n"
                f"MQTT: {self.mqtt_host} | Pairs: {n_pairs}\n"
                f"LD2412: {n_ld2412} | LD2450: {n_ld2450} | CSI: {n_csi} node(s)\n"
                f"Calibration: {cal_info}",
                force=True,
            )
        else:
            self.telegram.send(
                "\u26a0\ufe0f <b>Fusion MQTT reconnected</b>",
                force=True,
            )

    # -- Lifecycle --

    def run(self):
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        log.info("Connecting to MQTT %s:%d ...", self.mqtt_host, self.mqtt_port)
        self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)

        # Telegram command integration
        self.telegram._status_callback = self._telegram_status
        self.telegram._stats_callback = self._telegram_stats
        if self.ha_poller:
            self.telegram._gt_callback = self._telegram_ground_truth
        self.telegram._mqtt_client = self.client
        # Alarm command topic for first pair (if radar configured)
        first_pair = self.config["pairs"][0]
        ld2412_id = first_pair.get("ld2412_id", "")
        if ld2412_id:
            self.telegram._alarm_topic = f"security/{ld2412_id}/alarm/command"

        while self._running:
            self.client.loop(timeout=0.05)
            self.telegram.poll_commands()
            if self.ha_poller:
                self.ha_poller.poll()
            self._write_periodic_snapshots()
            self._flush_hourly_stats()
            self._check_snapshot_health()
            time.sleep(0.02)

        self.telegram.send(
            "\U0001f534 <b>Fusion service stopping</b>",
            force=True,
        )
        self.client.publish("security/fusion/availability", "offline", qos=1, retain=True)
        self.client.disconnect()
        self.db.close()
        log.info("Fusion service stopped.")

    def _telegram_status(self) -> str:
        """Build status string for /stav command."""
        parts = []
        uptime = time.time() - self._startup_time
        h, m = int(uptime // 3600), int((uptime % 3600) // 60)
        parts.append(f"\nUptime: {h}h {m}m")
        for name, result in self.last_results.items():
            parts.append(
                f"\n<b>{name}</b>: {'DETECTED' if result.presence else 'clear'}"
                f" ({result.confidence:.0%}, {result.source})"
            )
        n_cal = sum(1 for c in self.csi_calibration.values() if c.last_recalc > 0)
        parts.append(f"\nCSI kalibrace: {n_cal}/{len(self.csi_calibration)} node(s)")
        # DB summary
        try:
            stats = self.db.get_stats(hours=24)
            parts.append(f"\n24h: {stats['detections_24h']} detekci, "
                         f"avg conf {stats['avg_confidence']:.0%}")
            parts.append(f"\nDB: {stats['db_size_mb']:.1f} MB")
        except Exception:
            pass
        return "".join(parts)

    def _telegram_stats(self) -> str:
        """Build detailed stats string for /stats command."""
        try:
            stats = self.db.get_stats(hours=24)
        except Exception as e:
            return f"📊 Chyba: {e}"

        lines = ["📊 <b>Statistiky (24h)</b>"]
        lines.append(f"\nDetekci: {stats['detections_24h']}")
        lines.append(f"Avg confidence: {stats['avg_confidence']:.0%}")
        if stats.get("events"):
            ev_str = ", ".join(f"{k}:{v}" for k, v in stats["events"].items())
            lines.append(f"Eventy: {ev_str}")
        for node_id, cs in stats.get("csi", {}).items():
            short = node_id.replace("csi_", "")
            lines.append(
                f"\n<b>CSI {short}</b>: var={cs['avg_variance']:.4f} "
                f"turb={cs['avg_turbulence']:.4f} motion={cs['motion_pct']:.0f}%"
            )
        lines.append(f"\nAktivnich nodu: {stats.get('active_nodes', 0)}")
        lines.append(f"DB: {stats['db_size_mb']:.1f} MB")
        return "\n".join(lines)

    def _telegram_ground_truth(self) -> str:
        """Build ground truth accuracy report for /gt command."""
        try:
            gt_stats = self.db.get_ground_truth_stats(hours=24)
        except Exception as e:
            return f"📊 GT chyba: {e}"

        if not gt_stats:
            return "📊 <b>Ground Truth</b>\n\nZatim zadna data (cekam na prvni poll z HA)."

        lines = ["📊 <b>Ground Truth vs CSI (24h)</b>"]
        for room, rs in sorted(gt_stats.items()):
            lines.append(
                f"\n<b>{room}</b>: {rs['total']} vzorku"
                f"\n  Shoda: {rs['agreement_pct']:.0f}%"
                f"  FN: {rs['false_neg_pct']:.0f}%"
                f"  FP: {rs['false_pos_pct']:.0f}%"
            )
        return "\n".join(lines)

    def _shutdown(self, signum, frame):
        log.info("Shutdown signal received.")
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    log.info("Loading config from %s", config_path)
    service = FusionService(config_path)
    service.run()
