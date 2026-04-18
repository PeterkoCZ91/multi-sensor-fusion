#!/usr/bin/env python3
"""
deep_analysis.py — Comprehensive analysis of 470k+ sensor_snapshots.

Outputs:
  1. Ground truth accuracy (fusion vs MW Sonoff)
  2. Per-sensor contribution and correlation
  3. CSI feature distributions (detected vs clear)
  4. LD2450 failure mode analysis
  5. Confidence histogram
  6. FSM ping-pong analysis
  7. Hourly/daily patterns
  8. Per-pair breakdown

Usage:
    python3 tools/deep_analysis.py [--db data/fusion.db] [--pair obyvak]
"""

import argparse
import sqlite3
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


def parse_args():
    p = argparse.ArgumentParser(description="Deep fusion data analysis")
    p.add_argument("--db", default="data/fusion.db")
    p.add_argument("--pair", default=None, help="Filter to pair (default: all)")
    p.add_argument("--limit", type=int, default=0, help="Row limit (0=all)")
    return p.parse_args()


def fetch_snapshots(conn, pair=None, limit=0):
    sql = "SELECT * FROM sensor_snapshots"
    params = []
    if pair:
        sql += " WHERE pair_name = ?"
        params.append(pair)
    sql += " ORDER BY timestamp"
    if limit:
        sql += f" LIMIT {limit}"
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def parse_ha(ha_json):
    if not ha_json:
        return {}
    try:
        return json.loads(ha_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_csi(csi_json):
    if not csi_json:
        return {}
    try:
        return json.loads(csi_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def get_mw_ground_truth(ha, pair):
    """Microwave-sensor ground truth. Returns True/False/None.

    Populate `mw_keys` with the MQTT/HA entity IDs of a high-precision
    reference sensor (e.g. Sonoff microwave) per fusion pair. Leave empty
    to skip ground-truth comparison for a pair.
    """
    mw_keys: dict[str, str] = {
        # "living":   "binary_sensor.living_mw_occupancy",
        # "hallway":  "binary_sensor.hallway_mw_occupancy",
        # "bedroom":  "binary_sensor.bedroom_mw_occupancy",
    }
    key = mw_keys.get(pair)
    if not key:
        return None
    entry = ha.get(key)
    if entry is None:
        # Try partial match
        for k, v in ha.items():
            if key in k:
                entry = v
                break
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("on", False)
    return bool(entry)


# ---------------------------------------------------------------------------
# Analysis sections
# ---------------------------------------------------------------------------

def section_ground_truth(rows, pair_filter):
    """1. Ground truth accuracy: fusion_state vs MW."""
    print("\n" + "=" * 70)
    print("1. GROUND TRUTH ACCURACY (Fusion vs MW Sonoff)")
    print("=" * 70)

    pairs = set(r["pair_name"] for r in rows)
    for pair in sorted(pairs):
        if pair_filter and pair != pair_filter:
            continue
        pr = [r for r in rows if r["pair_name"] == pair]
        tp = fp = fn = tn = no_gt = 0
        for r in pr:
            ha = parse_ha(r.get("ha_sensors_json"))
            gt = get_mw_ground_truth(ha, pair)
            if gt is None:
                no_gt += 1
                continue
            detected = r["fusion_state"] == "detected"
            if detected and gt:
                tp += 1
            elif detected and not gt:
                fp += 1
            elif not detected and gt:
                fn += 1
            else:
                tn += 1

        total = tp + fp + fn + tn
        if total == 0:
            print(f"\n  {pair}: no ground truth data")
            continue

        acc = (tp + tn) / total
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0

        print(f"\n  {pair} ({total:,} snapshots, {no_gt:,} without GT):")
        print(f"    TP={tp:,}  FP={fp:,}  FN={fn:,}  TN={tn:,}")
        print(f"    Accuracy:  {acc:.1%}")
        print(f"    Precision: {prec:.1%}  (of detected, how many truly occupied)")
        print(f"    Recall:    {rec:.1%}  (of occupied, how many detected)")
        print(f"    F1:        {f1:.3f}")


def section_sensor_contribution(rows, pair_filter):
    """2. Per-sensor contribution — which sensors fire when GT=occupied."""
    print("\n" + "=" * 70)
    print("2. PER-SENSOR CONTRIBUTION (when MW=occupied)")
    print("=" * 70)

    pairs = set(r["pair_name"] for r in rows)
    for pair in sorted(pairs):
        if pair_filter and pair != pair_filter:
            continue
        pr = [r for r in rows if r["pair_name"] == pair]
        occupied = [r for r in pr
                    if get_mw_ground_truth(parse_ha(r.get("ha_sensors_json")), pair)]
        if not occupied:
            continue

        n = len(occupied)
        ld2412_on = sum(1 for r in occupied if r.get("ld2412_presence"))
        ld2450_on = sum(1 for r in occupied if r.get("ld2450_target_count", 0) > 0)
        csi_any = 0
        for r in occupied:
            csi = parse_csi(r.get("csi_json"))
            if any(v.get("ar", 0) >= 1.2 for v in csi.values() if isinstance(v, dict)):
                csi_any += 1

        # HA sensors
        ha_counts = Counter()
        for r in occupied:
            ha = parse_ha(r.get("ha_sensors_json"))
            for k, v in ha.items():
                if isinstance(v, dict) and v.get("on"):
                    ha_counts[k] += 1

        print(f"\n  {pair} ({n:,} occupied snapshots):")
        print(f"    LD2412 present:  {ld2412_on:>7,} ({ld2412_on/n:.1%})")
        print(f"    LD2450 targets:  {ld2450_on:>7,} ({ld2450_on/n:.1%})")
        print(f"    CSI ar>=1.2:     {csi_any:>7,} ({csi_any/n:.1%})")
        for k, c in ha_counts.most_common(10):
            short = k.split(".")[-1][:35] if "." in k else k[:35]
            print(f"    HA {short}: {c:>7,} ({c/n:.1%})")


def section_csi_features(rows, pair_filter):
    """3. CSI feature distributions (detected vs clear)."""
    print("\n" + "=" * 70)
    print("3. CSI FEATURE ANALYSIS (detected vs clear)")
    print("=" * 70)

    # Collect per-node ar, variance, phase_turbulence
    node_data = defaultdict(lambda: {"detected": [], "clear": []})

    for r in rows:
        if pair_filter and r["pair_name"] != pair_filter:
            continue
        csi = parse_csi(r.get("csi_json"))
        state = "detected" if r["fusion_state"] == "detected" else "clear"
        for node_id, metrics in csi.items():
            if not isinstance(metrics, dict):
                continue
            ar = metrics.get("ar", 0)
            var = metrics.get("var", 0)
            turb = metrics.get("turb", 0)
            phase = metrics.get("phase_turb", 0)
            if ar > 0:
                node_data[node_id][state].append({
                    "ar": ar, "var": var, "turb": turb, "phase": phase
                })

    for node_id in sorted(node_data.keys()):
        d = node_data[node_id]
        det = d["detected"]
        clr = d["clear"]
        if not det and not clr:
            continue

        print(f"\n  {node_id} (detected={len(det):,}, clear={len(clr):,}):")

        for label, samples in [("detected", det), ("clear", clr)]:
            if not samples:
                print(f"    {label}: no data")
                continue
            for metric in ["ar", "var", "turb", "phase"]:
                vals = [s[metric] for s in samples if s[metric] > 0]
                if not vals:
                    continue
                vals.sort()
                n = len(vals)
                avg = sum(vals) / n
                p50 = vals[n // 2]
                p90 = vals[int(n * 0.9)]
                p99 = vals[int(n * 0.99)]
                mx = vals[-1]
                print(f"    {label} {metric:>6}: avg={avg:.4f}  p50={p50:.4f}  "
                      f"p90={p90:.4f}  p99={p99:.4f}  max={mx:.4f}  (n={n:,})")


def section_ld2450_failure(rows, pair_filter):
    """4. LD2450 failure mode analysis."""
    print("\n" + "=" * 70)
    print("4. LD2450 FAILURE MODE ANALYSIS")
    print("=" * 70)

    pairs = set(r["pair_name"] for r in rows)
    for pair in sorted(pairs):
        if pair_filter and pair != pair_filter:
            continue
        pr = [r for r in rows if r["pair_name"] == pair]

        total = len(pr)
        has_target = sum(1 for r in pr if r.get("ld2450_target_count", 0) > 0)
        zero_target = total - has_target

        # When MW says occupied
        occupied = [r for r in pr
                    if get_mw_ground_truth(parse_ha(r.get("ha_sensors_json")), pair)]
        occ_has = sum(1 for r in occupied if r.get("ld2450_target_count", 0) > 0)
        occ_miss = len(occupied) - occ_has

        # Distance distribution when targeting
        dists = [r.get("ld2450_distance_mm", 0) for r in pr
                 if r.get("ld2450_target_count", 0) > 0 and r.get("ld2450_distance_mm", 0) > 0]

        # Target count distribution
        tc = Counter(r.get("ld2450_target_count", 0) for r in pr)

        # Check if ld2450_target_count is always 0 (never receives data)
        ever_nonzero = has_target > 0

        print(f"\n  {pair} ({total:,} snapshots):")
        print(f"    Has targets:  {has_target:>7,} ({has_target/total:.1%})")
        print(f"    Zero targets: {zero_target:>7,} ({zero_target/total:.1%})")
        if occupied:
            print(f"    When MW=occupied ({len(occupied):,}):")
            print(f"      LD2450 hit:  {occ_has:>7,} ({occ_has/len(occupied):.1%})")
            print(f"      LD2450 miss: {occ_miss:>7,} ({occ_miss/len(occupied):.1%})")
        print(f"    Target count distribution: {dict(sorted(tc.items()))}")
        if dists:
            dists.sort()
            n = len(dists)
            print(f"    Distance when targeting: "
                  f"p10={dists[int(n*0.1)]:.0f}mm  p50={dists[n//2]:.0f}mm  "
                  f"p90={dists[int(n*0.9)]:.0f}mm  max={dists[-1]:.0f}mm")
        if not ever_nonzero:
            print(f"    *** LD2450 NEVER had targets — likely not receiving MQTT data! ***")


def section_confidence_histogram(rows, pair_filter):
    """5. Confidence distribution."""
    print("\n" + "=" * 70)
    print("5. CONFIDENCE HISTOGRAM")
    print("=" * 70)

    buckets = defaultdict(int)
    for r in rows:
        if pair_filter and r["pair_name"] != pair_filter:
            continue
        c = r.get("fusion_confidence", 0)
        bucket = round(c, 1)
        buckets[bucket] += 1

    total = sum(buckets.values())
    print(f"\n  Total: {total:,}")
    for b in sorted(buckets.keys()):
        cnt = buckets[b]
        bar = "#" * int(cnt / total * 100)
        print(f"    {b:.1f}: {cnt:>8,} ({cnt/total:>5.1%}) {bar}")

    # Source distribution per confidence level
    print("\n  Source distribution by confidence:")
    src_by_conf = defaultdict(lambda: Counter())
    for r in rows:
        if pair_filter and r["pair_name"] != pair_filter:
            continue
        c = round(r.get("fusion_confidence", 0), 1)
        src = r.get("fusion_source", "none")
        src_by_conf[c][src] += 1

    for b in sorted(src_by_conf.keys()):
        top3 = src_by_conf[b].most_common(3)
        top_str = ", ".join(f"{s}={n:,}" for s, n in top3)
        print(f"    {b:.1f}: {top_str}")


def section_fsm_pingpong(rows, pair_filter):
    """6. FSM state transitions and ping-pong analysis."""
    print("\n" + "=" * 70)
    print("6. FSM STABILITY ANALYSIS")
    print("=" * 70)

    pairs = set(r["pair_name"] for r in rows)
    for pair in sorted(pairs):
        if pair_filter and pair != pair_filter:
            continue
        pr = [r for r in rows if r["pair_name"] == pair]
        pr.sort(key=lambda r: r["timestamp"])

        # State distribution
        states = Counter(r.get("fsm_state", "?") for r in pr)
        print(f"\n  {pair} — FSM state distribution:")
        for s, c in states.most_common():
            print(f"    {s:>12}: {c:>8,} ({c/len(pr):.1%})")

        # Transitions
        transitions = Counter()
        pingpong_30 = 0
        pingpong_60 = 0
        state_durations = defaultdict(list)
        last_state = None
        last_ts = None

        for r in pr:
            st = r.get("fsm_state", "?")
            ts = r["timestamp"]
            if last_state is not None and st != last_state:
                transitions[(last_state, st)] += 1
                if last_ts:
                    state_durations[last_state].append(ts - last_ts)
                last_ts = ts
            elif last_state is None:
                last_ts = ts
            last_state = st

        # Count ping-pong: present→clearing→present in sequence
        seq = [(r.get("fsm_state"), r["timestamp"]) for r in pr]
        for i in range(len(seq) - 2):
            s0, t0 = seq[i]
            s1, t1 = seq[i + 1]
            s2, t2 = seq[i + 2]
            if s0 == "present" and s1 == "clearing" and s2 == "present":
                dur = t2 - t0
                if dur < 60:
                    pingpong_60 += 1
                if dur < 30:
                    pingpong_30 += 1

        print(f"\n  {pair} — State transitions:")
        for (a, b), c in transitions.most_common(10):
            print(f"    {a:>12} → {b:<12}: {c:>6,}")

        print(f"\n  {pair} — Ping-pong (present→clearing→present):")
        print(f"    Within 30s: {pingpong_30:,}")
        print(f"    Within 60s: {pingpong_60:,}")

        # Median state durations
        print(f"\n  {pair} — State durations (seconds):")
        for st, durs in sorted(state_durations.items()):
            if not durs:
                continue
            durs.sort()
            n = len(durs)
            med = durs[n // 2]
            p10 = durs[int(n * 0.1)]
            p90 = durs[int(n * 0.9)]
            print(f"    {st:>12}: median={med:.1f}s  p10={p10:.1f}s  p90={p90:.1f}s  (n={n:,})")


def section_hourly_pattern(rows, pair_filter):
    """7. Detection rate by hour of day."""
    print("\n" + "=" * 70)
    print("7. HOURLY DETECTION PATTERN")
    print("=" * 70)

    hour_total = defaultdict(int)
    hour_detected = defaultdict(int)
    for r in rows:
        if pair_filter and r["pair_name"] != pair_filter:
            continue
        ts = r["timestamp"]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        # Convert to Prague time (UTC+1/+2)
        h = (dt.hour + 2) % 24  # approximate CEST
        hour_total[h] += 1
        if r["fusion_state"] == "detected":
            hour_detected[h] += 1

    print(f"\n  {'Hour':>6} {'Total':>8} {'Detected':>8} {'Rate':>6}  Bar")
    for h in range(24):
        t = hour_total.get(h, 0)
        d = hour_detected.get(h, 0)
        rate = d / t if t else 0
        bar = "#" * int(rate * 50)
        print(f"  {h:>4}:00 {t:>8,} {d:>8,} {rate:>5.1%}  {bar}")


def section_data_quality(rows, pair_filter):
    """8. Data quality — NULLs, gaps, missing sensors."""
    print("\n" + "=" * 70)
    print("8. DATA QUALITY")
    print("=" * 70)

    total = len(rows)
    csi_null = sum(1 for r in rows if not r.get("csi_json")
                   and (not pair_filter or r["pair_name"] == pair_filter))
    ha_null = sum(1 for r in rows if not r.get("ha_sensors_json")
                  and (not pair_filter or r["pair_name"] == pair_filter))
    ld2450_zero = sum(1 for r in rows
                      if r.get("ld2450_target_count", 0) == 0
                      and (not pair_filter or r["pair_name"] == pair_filter))
    ld2412_zero = sum(1 for r in rows
                      if not r.get("ld2412_presence")
                      and (not pair_filter or r["pair_name"] == pair_filter))

    # Time gaps > 30s between snapshots
    gaps = []
    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair_name"]].append(r["timestamp"])

    for pair, ts_list in by_pair.items():
        if pair_filter and pair != pair_filter:
            continue
        ts_list.sort()
        for i in range(1, len(ts_list)):
            gap = ts_list[i] - ts_list[i - 1]
            if gap > 60:  # >1min gap
                gaps.append((pair, ts_list[i - 1], gap))

    eff_total = sum(1 for r in rows if not pair_filter or r["pair_name"] == pair_filter)
    print(f"\n  Total snapshots: {eff_total:,}")
    print(f"  CSI data missing:   {csi_null:>8,} ({csi_null/eff_total:.1%})")
    print(f"  HA data missing:    {ha_null:>8,} ({ha_null/eff_total:.1%})")
    print(f"  LD2450 zero:        {ld2450_zero:>8,} ({ld2450_zero/eff_total:.1%})")
    print(f"  LD2412 not present: {ld2412_zero:>8,} ({ld2412_zero/eff_total:.1%})")

    if gaps:
        print(f"\n  Time gaps >1 min: {len(gaps)}")
        gaps.sort(key=lambda g: -g[2])
        for pair, ts, gap in gaps[:10]:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            print(f"    {pair}: {dt:%Y-%m-%d %H:%M} — gap {gap/60:.0f} min")


def section_events_check(conn):
    """Check events table status."""
    print("\n" + "=" * 70)
    print("9. EVENTS TABLE STATUS")
    print("=" * 70)

    row = conn.execute("SELECT count(*), min(timestamp), max(timestamp) FROM events").fetchone()
    cnt, first, last = row
    print(f"\n  Events: {cnt:,}")
    if first:
        dt_first = datetime.fromtimestamp(first, tz=timezone.utc)
        dt_last = datetime.fromtimestamp(last, tz=timezone.utc)
        print(f"  First: {dt_first:%Y-%m-%d %H:%M}")
        print(f"  Last:  {dt_last:%Y-%m-%d %H:%M}")
        # Check gap
        snap_last = conn.execute("SELECT max(timestamp) FROM sensor_snapshots").fetchone()[0]
        if snap_last and last:
            gap_days = (snap_last - last) / 86400
            if gap_days > 1:
                print(f"  *** WARNING: Events stopped {gap_days:.1f} days before latest snapshot! ***")
                print(f"      Events write is tied to _publish_fusion() — check publish rate-limiting")

    # Event type distribution
    rows = conn.execute(
        "SELECT event_type, count(*) FROM events GROUP BY event_type ORDER BY count(*) DESC"
    ).fetchall()
    print(f"\n  Event types:")
    for et, c in rows:
        print(f"    {et}: {c:,}")

    # Source distribution
    src_rows = conn.execute(
        "SELECT source, count(*) as c FROM events GROUP BY source ORDER BY c DESC LIMIT 15"
    ).fetchall()
    print(f"\n  Top sources:")
    for src, c in src_rows:
        print(f"    {src}: {c:,}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print(f"Loading snapshots from {args.db}...")
    rows = fetch_snapshots(conn, args.pair, args.limit)
    print(f"Loaded {len(rows):,} snapshots")

    if not rows:
        print("No data!")
        sys.exit(1)

    # Convert Row to dict for easier access
    rows = [dict(r) for r in rows]

    ts_first = datetime.fromtimestamp(rows[0]["timestamp"], tz=timezone.utc)
    ts_last = datetime.fromtimestamp(rows[-1]["timestamp"], tz=timezone.utc)
    print(f"Time range: {ts_first:%Y-%m-%d %H:%M} — {ts_last:%Y-%m-%d %H:%M}")
    print(f"Pairs: {sorted(set(r['pair_name'] for r in rows))}")

    section_ground_truth(rows, args.pair)
    section_sensor_contribution(rows, args.pair)
    section_csi_features(rows, args.pair)
    section_ld2450_failure(rows, args.pair)
    section_confidence_histogram(rows, args.pair)
    section_fsm_pingpong(rows, args.pair)
    section_hourly_pattern(rows, args.pair)
    section_data_quality(rows, args.pair)
    section_events_check(conn)

    conn.close()
    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
