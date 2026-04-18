#!/usr/bin/env python3
"""
analyze_weights.py — Fusion FP/FN analysis and distance weight validation.

Joins sensor_snapshots with ha_ground_truth to find:
  - False Positives: fusion=detected but GT=off
  - False Negatives: fusion=clear but GT=on

Outputs histograms of FP distance distribution and suggests optimal
distance_weight thresholds for each pair.

Usage:
    python3 tools/analyze_weights.py [--db path/to/fusion.db] [--days 7] [--pair obyvak]
"""

import argparse
import sqlite3
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Fusion weight analysis")
    p.add_argument("--db", default="data/fusion.db", help="Path to fusion.db")
    p.add_argument("--days", type=int, default=7, help="Look-back window in days")
    p.add_argument("--pair", default=None, help="Filter to specific pair (default: all)")
    p.add_argument("--gt-window-s", type=float, default=5.0,
                   help="Max seconds between snapshot and GT sample to correlate (default: 5)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def fetch_snapshots(conn: sqlite3.Connection, since: float, pair: str | None) -> list[dict]:
    sql = """
        SELECT timestamp, pair_name, fusion_state, fusion_confidence,
               fusion_source, ld2450_target_count, ld2450_distance_mm,
               ld2412_presence, ld2412_distance_cm, ld2450_targets_json
        FROM sensor_snapshots
        WHERE timestamp >= ?
    """
    params: list = [since]
    if pair:
        sql += " AND pair_name = ?"
        params.append(pair)
    sql += " ORDER BY timestamp"
    rows = conn.execute(sql, params).fetchall()
    cols = ["timestamp", "pair_name", "fusion_state", "fusion_confidence",
            "fusion_source", "ld2450_target_count", "ld2450_distance_mm",
            "ld2412_presence", "ld2412_distance_cm", "ld2450_targets_json"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_ground_truth(conn: sqlite3.Connection, since: float, room: str | None) -> list[dict]:
    sql = """
        SELECT timestamp, room, entity_id, sensor_type, state, fusion_confidence
        FROM ha_ground_truth
        WHERE timestamp >= ?
    """
    params: list = [since]
    if room:
        sql += " AND room = ?"
        params.append(room)
    sql += " ORDER BY timestamp"
    rows = conn.execute(sql, params).fetchall()
    cols = ["timestamp", "room", "entity_id", "sensor_type", "state", "fusion_confidence"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def build_gt_index(gt_rows: list[dict], room: str) -> list[tuple[float, str]]:
    """Return sorted list of (timestamp, state) for one room's GT."""
    entries = [(r["timestamp"], r["state"]) for r in gt_rows if r["room"] == room]
    entries.sort()
    return entries


def gt_state_at(gt_index: list[tuple[float, str]], ts: float,
                window_s: float) -> str | None:
    """Binary-search GT index for the closest entry within window_s of ts."""
    if not gt_index:
        return None
    lo, hi = 0, len(gt_index) - 1
    best = None
    best_diff = float("inf")
    while lo <= hi:
        mid = (lo + hi) // 2
        diff = abs(gt_index[mid][0] - ts)
        if diff < best_diff:
            best_diff = diff
            best = gt_index[mid][1]
        if gt_index[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid - 1
    return best if best_diff <= window_s else None


# ---------------------------------------------------------------------------
# Distance bucketing
# ---------------------------------------------------------------------------

BUCKETS = [
    (0,    1500,  "<1.5m"),
    (1500, 2500,  "1.5-2.5m"),
    (2500, 3500,  "2.5-3.5m (phantom zone)"),
    (3500, 4500,  "3.5-4.5m"),
    (4500, 99999, ">4.5m"),
]

def distance_bucket(mm: float | None) -> str:
    if mm is None or mm <= 0:
        return "unknown"
    for lo, hi, label in BUCKETS:
        if lo <= mm < hi:
            return label
    return ">4.5m"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_pair(pair_name: str, snapshots: list[dict], gt_rows: list[dict],
                 gt_window_s: float):
    gt_idx = build_gt_index(gt_rows, pair_name)
    if not gt_idx:
        print(f"\n  [WARN] No ground truth data for '{pair_name}' — skipping correlation.")

    total = len(snapshots)
    fp_by_bucket: dict[str, int] = defaultdict(int)
    fn_by_bucket: dict[str, int] = defaultdict(int)
    total_by_bucket: dict[str, int] = defaultdict(int)
    correlated = 0
    fp_sources: dict[str, int] = defaultdict(int)
    fp_confidences: list[float] = []

    # Source breakdown for all detected events
    source_detected: dict[str, int] = defaultdict(int)
    source_total: dict[str, int] = defaultdict(int)

    for snap in snapshots:
        fusion_detected = snap["fusion_state"] in ("detected", "present", "PRESENT")
        dist_mm = snap["ld2450_distance_mm"] or 0.0
        bucket = distance_bucket(dist_mm if dist_mm > 0 else None)
        total_by_bucket[bucket] += 1

        source = snap["fusion_source"] or ""
        source_total[source] += 1
        if fusion_detected:
            source_detected[source] += 1

        gt = gt_state_at(gt_idx, snap["timestamp"], gt_window_s)
        if gt is None:
            continue
        correlated += 1
        gt_on = (gt == "on")

        if fusion_detected and not gt_on:
            # False Positive
            fp_by_bucket[bucket] += 1
            fp_sources[source] += 1
            fp_confidences.append(snap["fusion_confidence"] or 0.0)
        elif not fusion_detected and gt_on:
            # False Negative
            fn_by_bucket[bucket] += 1

    print(f"\n{'='*60}")
    print(f"  Pair: {pair_name}  |  Snapshots: {total}  |  Correlated with GT: {correlated}")
    print(f"{'='*60}")

    print("\n  --- Detected events by source ---")
    for src, cnt in sorted(source_detected.items(), key=lambda x: -x[1]):
        tot = source_total.get(src, cnt)
        pct = 100 * cnt / tot if tot else 0
        print(f"    {src or '(none)':40s}  detected={cnt:5d}/{tot:5d}  ({pct:.0f}%)")

    if correlated == 0:
        print("\n  [WARN] No correlated GT+snapshot pairs found.")
        print("         Check that GT entities match pair name and --gt-window-s.")
        return

    total_fp = sum(fp_by_bucket.values())
    total_fn = sum(fn_by_bucket.values())
    fp_rate = 100 * total_fp / correlated if correlated else 0
    fn_rate = 100 * total_fn / correlated if correlated else 0

    print(f"\n  --- Overall (correlated={correlated}) ---")
    print(f"    False Positives (fusion=ON, GT=OFF):  {total_fp:5d}  ({fp_rate:.1f}%)")
    print(f"    False Negatives (fusion=OFF, GT=ON):  {total_fn:5d}  ({fn_rate:.1f}%)")

    if total_fp:
        avg_fp_conf = sum(fp_confidences) / len(fp_confidences)
        print(f"    Avg FP confidence: {avg_fp_conf:.3f}")

    print("\n  --- FP distribution by distance ---")
    print(f"    {'Bucket':30s}  {'FPs':>6}  {'Snaps':>6}  {'FP%':>6}")
    print(f"    {'-'*55}")
    for lo, hi, label in BUCKETS:
        fps = fp_by_bucket.get(label, 0)
        snaps = total_by_bucket.get(label, 0)
        pct = 100 * fps / snaps if snaps else 0
        flag = " ← high FP" if pct > 30 else ""
        print(f"    {label:30s}  {fps:6d}  {snaps:6d}  {pct:5.1f}%{flag}")
    unk_fps = fp_by_bucket.get("unknown", 0)
    unk_tot = total_by_bucket.get("unknown", 0)
    if unk_tot:
        pct = 100 * unk_fps / unk_tot if unk_tot else 0
        print(f"    {'unknown':30s}  {unk_fps:6d}  {unk_tot:6d}  {pct:5.1f}%")

    print("\n  --- FP by source ---")
    for src, cnt in sorted(fp_sources.items(), key=lambda x: -x[1]):
        print(f"    {src or '(none)':40s}  {cnt:5d} FPs")

    # Suggestions
    print("\n  --- Suggested distance_weight adjustments ---")
    for lo, hi, label in BUCKETS:
        fps = fp_by_bucket.get(label, 0)
        snaps = total_by_bucket.get(label, 0)
        if snaps < 10:
            continue
        fp_pct = fps / snaps
        if fp_pct > 0.4:
            mid_m = (lo + hi) / 2 / 1000
            # Current weights from _ld2450_distance_weight
            current = {0.75: 0.40, 2.0: 0.35, 3.0: 0.25, 4.0: 0.20, 99: 0.12}.get(
                min(k for k in [0.75, 2.0, 3.0, 4.0, 99] if k >= mid_m), 0.12)
            suggested = max(current - 0.10, 0.05)
            print(f"    {label}: FP={fp_pct:.0%} → consider reducing weight "
                  f"{current:.2f} → {suggested:.2f}")

    if not any(fp_by_bucket.get(l, 0) > 0 for _, _, l in BUCKETS):
        print("    All FP rates within acceptable range — no changes needed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()

    print(f"Fusion Weight Analysis")
    print(f"  DB:      {args.db}")
    print(f"  Period:  last {args.days} days (since {datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M')})")
    print(f"  Pair:    {args.pair or 'all'}")
    print(f"  GT corr window: ±{args.gt_window_s}s")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    snapshots = fetch_snapshots(conn, since, args.pair)
    gt_rows = fetch_ground_truth(conn, since, args.pair)

    if not snapshots:
        print("\n[ERROR] No sensor_snapshots found. Check --db path and --days range.")
        return

    print(f"\nLoaded {len(snapshots)} snapshots, {len(gt_rows)} GT rows")

    # Group snapshots by pair
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for snap in snapshots:
        by_pair[snap["pair_name"]].append(snap)

    for pair_name, pair_snaps in sorted(by_pair.items()):
        analyze_pair(pair_name, pair_snaps, gt_rows, args.gt_window_s)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
