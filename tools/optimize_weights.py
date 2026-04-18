#!/usr/bin/env python3
"""
optimize_weights.py — Data-driven weight optimization for fusion engine.

Uses sensor_snapshots with MW Sonoff as ground truth to find optimal
sensor weights via logistic regression and grid search for FSM parameters.

Usage:
    python3 tools/optimize_weights.py [--db data/fusion.db] [--pair obyvak]
"""

import argparse
import sqlite3
import json
import sys
import math
from collections import defaultdict


def parse_args():
    p = argparse.ArgumentParser(description="Optimize fusion weights from data")
    p.add_argument("--db", default="data/fusion.db")
    p.add_argument("--pair", default="obyvak")
    return p.parse_args()


# Ground-truth labels: entity_id of a high-precision reference sensor
# (e.g. Sonoff microwave) per fusion pair. Override via CLI or edit here.
MW_KEYS: dict[str, str] = {
    # "living":   "binary_sensor.living_mw_occupancy",
    # "hallway":  "binary_sensor.hallway_mw_occupancy",
    # "bedroom":  "binary_sensor.bedroom_mw_occupancy",
}


def load_features(conn, pair, sample_rate=20):
    """Extract features and ground truth from sensor_snapshots.
    sample_rate: take every Nth row (1=all, 5=20% sample for speed)
    """
    cur = conn.execute("""
        SELECT timestamp, fusion_state, fusion_confidence, fusion_source,
               ld2412_presence, ld2412_distance_cm,
               ld2450_target_count, ld2450_distance_mm,
               csi_json, ha_sensors_json, fsm_state
        FROM sensor_snapshots
        WHERE pair_name = ?
        ORDER BY timestamp
    """, (pair,))
    cols = [d[0] for d in cur.description]

    features = []
    labels = []
    mw_key = MW_KEYS.get(pair)
    row_idx = 0

    for row in cur:
        row_idx += 1
        if row_idx % sample_rate != 0:
            continue
        r = dict(zip(cols, row))

        # Ground truth from MW
        ha = {}
        if r["ha_sensors_json"]:
            try:
                ha = json.loads(r["ha_sensors_json"])
            except (json.JSONDecodeError, TypeError):
                continue

        gt_entry = ha.get(mw_key)
        if gt_entry is None:
            continue
        gt = gt_entry.get("on", False) if isinstance(gt_entry, dict) else bool(gt_entry)

        # Extract features
        ld2412 = 1.0 if r["ld2412_presence"] else 0.0
        ld2450_count = min(r["ld2450_target_count"] or 0, 3) / 3.0
        ld2450_dist = min((r["ld2450_distance_mm"] or 0) / 5000.0, 1.0)

        # CSI features
        csi_var = 0.0
        csi_ar = 0.0
        csi_turb = 0.0
        if r["csi_json"]:
            try:
                csi = json.loads(r["csi_json"])
                for node, metrics in csi.items():
                    if isinstance(metrics, dict):
                        csi_var = max(csi_var, metrics.get("var", 0))
                        csi_ar = max(csi_ar, metrics.get("ar", 0))
                        csi_turb = max(csi_turb, metrics.get("turb", 0))
            except (json.JSONDecodeError, TypeError):
                pass

        # HA sensors (excluding MW ground truth)
        ha_pir = 0.0
        ha_camera_motion = 0.0
        ha_camera_person = 0.0
        for k, v in ha.items():
            if k == mw_key:
                continue
            if isinstance(v, dict) and v.get("on"):
                vtype = v.get("type", "")
                if "pir" in vtype or "pir" in k:
                    ha_pir = 1.0
                elif "camera_motion" in vtype or "camera_motion" in k:
                    ha_camera_motion = 1.0
                elif "camera_person" in vtype or "camera_person" in k:
                    ha_camera_person = 1.0

        features.append([
            ld2412,             # 0: LD2412 presence
            ld2450_count,       # 1: LD2450 target count (normalized)
            min(csi_var, 1.0),  # 2: CSI variance (capped)
            min(csi_ar / 3.0, 1.0),  # 3: CSI anomaly ratio (normalized)
            min(csi_turb / 5.0, 1.0),  # 4: CSI turbulence (normalized)
            ha_pir,             # 5: PIR motion
            ha_camera_motion,   # 6: Camera motion
            ha_camera_person,   # 7: Camera person
        ])
        labels.append(1.0 if gt else 0.0)

    return features, labels


def logistic_regression(features, labels, lr=0.01, epochs=200, l2=0.01, batch_size=512):
    """Mini-batch SGD logistic regression with L2 regularization."""
    import random
    n_features = len(features[0])
    weights = [0.0] * n_features
    bias = 0.0
    n = len(features)
    indices = list(range(n))

    for epoch in range(epochs):
        random.shuffle(indices)
        total_loss = 0.0
        n_batches = 0

        for start in range(0, n, batch_size):
            batch = indices[start:start + batch_size]
            bs = len(batch)
            dw = [0.0] * n_features
            db = 0.0

            for i in batch:
                z = sum(w * f for w, f in zip(weights, features[i])) + bias
                z = max(min(z, 20), -20)
                pred = 1.0 / (1.0 + math.exp(-z))
                error = pred - labels[i]
                total_loss += -(labels[i] * math.log(max(pred, 1e-7)) +
                               (1 - labels[i]) * math.log(max(1 - pred, 1e-7)))
                for j in range(n_features):
                    dw[j] += error * features[i][j]
                db += error

            for j in range(n_features):
                weights[j] -= lr * (dw[j] / bs + l2 * weights[j])
            bias -= lr * db / bs
            n_batches += 1

        if (epoch + 1) % 50 == 0:
            avg_loss = total_loss / n
            print(f"  Epoch {epoch+1}: loss={avg_loss:.4f}")

    return weights, bias


def evaluate(features, labels, weights, bias, threshold=0.5):
    """Evaluate logistic regression predictions."""
    tp = fp = fn = tn = 0
    for i in range(len(features)):
        z = sum(w * f for w, f in zip(weights, features[i])) + bias
        z = max(min(z, 20), -20)
        pred = 1.0 / (1.0 + math.exp(-z))
        predicted = pred >= threshold
        actual = labels[i] >= 0.5
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1

    total = tp + fp + fn + tn
    acc = (tp + tn) / total if total else 0
    prec = tp / (tp + fp) if (tp + fp) else 0
    rec = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "accuracy": acc, "precision": prec, "recall": rec, "f1": f1}


def main():
    args = parse_args()
    conn = sqlite3.connect(args.db)

    print(f"Loading features for pair '{args.pair}'...")
    features, labels = load_features(conn, args.pair)
    conn.close()

    if not features:
        print("No data!")
        sys.exit(1)

    n = len(features)
    pos = sum(labels)
    neg = n - pos
    print(f"Samples: {n:,} (positive={pos:,.0f}, negative={neg:,.0f}, ratio={pos/n:.1%})")

    feature_names = [
        "LD2412 presence", "LD2450 targets", "CSI variance",
        "CSI anomaly_ratio", "CSI turbulence", "PIR motion",
        "Camera motion", "Camera person"
    ]

    # Feature statistics
    print("\n--- Feature Statistics ---")
    for j, name in enumerate(feature_names):
        vals_pos = [features[i][j] for i in range(n) if labels[i] >= 0.5]
        vals_neg = [features[i][j] for i in range(n) if labels[i] < 0.5]
        mean_pos = sum(vals_pos) / len(vals_pos) if vals_pos else 0
        mean_neg = sum(vals_neg) / len(vals_neg) if vals_neg else 0
        print(f"  {name:>20}: occupied_mean={mean_pos:.3f}  empty_mean={mean_neg:.3f}  "
              f"delta={mean_pos - mean_neg:+.3f}")

    # Train logistic regression
    print("\n--- Logistic Regression ---")
    weights, bias = logistic_regression(features, labels, lr=0.05, epochs=1000)

    print(f"\n  Bias: {bias:.4f}")
    print(f"\n  Learned weights (feature importance):")
    ranked = sorted(zip(feature_names, weights), key=lambda x: -abs(x[1]))
    for name, w in ranked:
        direction = "+" if w > 0 else "-"
        print(f"    {name:>20}: {w:+.4f} ({direction})")

    # Evaluate at different thresholds
    print("\n--- Evaluation at different thresholds ---")
    for thr in [0.3, 0.4, 0.5, 0.6, 0.7]:
        metrics = evaluate(features, labels, weights, bias, threshold=thr)
        print(f"  threshold={thr:.1f}: "
              f"Acc={metrics['accuracy']:.1%} "
              f"Prec={metrics['precision']:.1%} "
              f"Rec={metrics['recall']:.1%} "
              f"F1={metrics['f1']:.3f}")

    # Recommended weights for config
    print("\n--- Recommended sensor_scores weights ---")
    # Normalize weights to sum to 1.0
    abs_sum = sum(abs(w) for w in weights)
    if abs_sum > 0:
        norm = [abs(w) / abs_sum for w in weights]
        print("  (normalized absolute weights — use as sensor_scores weight parameter)")
        for name, w, nw in zip(feature_names, weights, norm):
            if nw > 0.02:
                print(f"    {name:>20}: weight={nw:.3f}  (raw={w:+.4f})")


if __name__ == "__main__":
    main()
