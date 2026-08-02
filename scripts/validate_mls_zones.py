#!/usr/bin/env python3
"""
Validate zone inference methods via held-out MLS label cross-validation.

The MLS school labels are current ground truth — listing agents declare
the assigned school under disclosure obligations. We split the labeled
points 80/20, train the classifier on 80%, and measure how well it
predicts the held-out 20%.

Compares three classifiers:
  1. Voronoi (nearest school) — geometric baseline
  2. Pure NN (nearest training sample)
  3. Bayesian (Voronoi prior + training sample evidence)

Reports accuracy overall and by sample density per district.

Usage:
    python scripts/validate_mls_zones.py --state MA
    python scripts/validate_mls_zones.py --state MA --folds 5
    python scripts/validate_mls_zones.py --state MA --level middle
"""

import argparse
import math
import os
import random
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

HERE = os.path.dirname(os.path.abspath(__file__))


def _haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def _nearest_school(lat, lon, schools):
    """Voronoi: assign to nearest school."""
    best = None
    best_d = float('inf')
    for nc, slat, slon in schools:
        d = _haversine(lat, lon, slat, slon)
        if d < best_d:
            best_d = d
            best = nc
    return best


def _nearest_sample(lat, lon, train_samples, max_miles=1.0):
    """Pure NN: nearest training sample."""
    best = None
    best_d = max_miles
    for slat, slon, snc in train_samples:
        d = _haversine(lat, lon, slat, slon)
        if d <= best_d:
            best_d = d
            best = snc
    return best


def _knn(lat, lon, train_samples, k=5, max_miles=1.0):
    """k-NN: majority vote of k nearest training samples."""
    scored = []
    for slat, slon, snc in train_samples:
        d = _haversine(lat, lon, slat, slon)
        if d <= max_miles:
            scored.append((d, snc))
    if not scored:
        return None, 0.0
    scored.sort()
    neighbors = scored[:k]
    votes = defaultdict(int)
    for _, snc in neighbors:
        votes[snc] += 1
    best_nc = max(votes, key=votes.get)
    agreement = votes[best_nc] / len(neighbors)
    return best_nc, agreement


def _knn_confidence(lat, lon, schools, train_samples,
                    k=5, max_miles=1.0):
    """k-NN with confidence score.

    Returns (ncessch, confidence) where confidence combines:
      - agreement: fraction of k neighbors that agree (0-1)
      - proximity: how close the nearest agreeing sample is (exp decay)
      - density: how many samples are within radius (log scale)

    confidence = agreement × proximity × density_factor
    """
    scored = []
    for slat, slon, snc in train_samples:
        d = _haversine(lat, lon, slat, slon)
        if d <= max_miles:
            scored.append((d, snc))
    if not scored:
        # Fall back to Voronoi
        return _nearest_school(lat, lon, schools), 0.0

    scored.sort()
    neighbors = scored[:k]
    votes = defaultdict(int)
    for _, snc in neighbors:
        votes[snc] += 1
    best_nc = max(votes, key=votes.get)

    agreement = votes[best_nc] / len(neighbors)

    nearest_agree = next(d for d, nc in scored if nc == best_nc)
    proximity = math.exp(-nearest_agree / 0.3)

    density = min(1.0, math.log1p(len(scored)) / math.log1p(20))

    confidence = agreement * proximity * density
    return best_nc, round(confidence, 3)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--level', default='elementary')
    ap.add_argument('--folds', type=int, default=5,
                    help='k-fold cross-validation (default 5)')
    ap.add_argument('--sigma', type=float, default=1.5)
    ap.add_argument('--lam', type=float, default=0.4)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    conn = sqlite3.connect(db.DB_PATH)

    # Load all MLS samples for this level, grouped by district
    rows = conn.execute(
        'SELECT zs.lat, zs.lon, zs.ncessch, s.leaid '
        'FROM zone_samples zs '
        'JOIN schools s ON zs.ncessch = s.ncessch '
        'WHERE s.state = ? AND s.level = ? AND zs.ncessch IS NOT NULL',
        (args.state, args.level)).fetchall()

    by_district = defaultdict(list)
    for lat, lon, nc, leaid in rows:
        by_district[leaid].append((lat, lon, nc))

    # Only test districts with ≥2 schools and ≥5 samples
    schools_by_district = {}
    testable = {}
    for leaid, samples in by_district.items():
        schools = conn.execute(
            'SELECT s.ncessch, s.lat, s.lon FROM schools s '
            'LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
            'WHERE s.leaid = ? AND s.level = ? '
            "AND COALESCE(r.source, '') NOT IN ('not-rated-alt', 'not-rated-pk') "
            'AND s.lat IS NOT NULL AND s.lon IS NOT NULL',
            (leaid, args.level)).fetchall()
        if len(schools) >= 2 and len(samples) >= 5:
            # Only keep samples that reference schools in our candidate set
            valid_ncs = {s[0] for s in schools}
            valid_samples = [s for s in samples if s[2] in valid_ncs]
            if len(valid_samples) >= 5:
                testable[leaid] = valid_samples
                schools_by_district[leaid] = schools

    total_samples = sum(len(s) for s in testable.values())
    print(f"Testable: {len(testable)} districts, {total_samples} samples "
          f"({args.level}, {args.folds}-fold CV)")

    METHODS = ['voronoi', '1-nn', '5-nn', '5-nn-conf']
    totals = {m: [0, 0] for m in METHODS}  # [correct, tested]
    buckets = {bk: {m: [0, 0] for m in METHODS}
               for bk in ('5-15', '16-50', '51-100', '101+')}
    per_district = []

    # For confidence calibration
    conf_bins = {f"{lo:.1f}-{lo+0.1:.1f}": [0, 0]
                 for lo in [i/10 for i in range(10)]}

    for leaid, samples in testable.items():
        schools = schools_by_district[leaid]
        n = len(samples)
        random.shuffle(samples)

        d = {m: [0, 0] for m in METHODS}

        fold_size = max(1, n // args.folds)
        for fold in range(args.folds):
            start = fold * fold_size
            end = start + fold_size if fold < args.folds - 1 else n
            test = samples[start:end]
            train = samples[:start] + samples[end:]

            for lat, lon, true_nc in test:
                # Voronoi
                vor = _nearest_school(lat, lon, schools)
                d['voronoi'][1] += 1
                if vor == true_nc:
                    d['voronoi'][0] += 1

                # 1-NN
                nn1 = _nearest_sample(lat, lon, train)
                if nn1 is not None:
                    d['1-nn'][1] += 1
                    if nn1 == true_nc:
                        d['1-nn'][0] += 1

                # 5-NN (majority vote)
                knn5, agreement = _knn(lat, lon, train, k=5)
                if knn5 is not None:
                    d['5-nn'][1] += 1
                    if knn5 == true_nc:
                        d['5-nn'][0] += 1

                # 5-NN with confidence
                knn5c, conf = _knn_confidence(lat, lon, schools, train, k=5)
                d['5-nn-conf'][1] += 1
                if knn5c == true_nc:
                    d['5-nn-conf'][0] += 1

                # Track confidence calibration
                bin_lo = min(int(conf * 10), 9) / 10
                bin_key = f"{bin_lo:.1f}-{bin_lo+0.1:.1f}"
                if bin_key in conf_bins:
                    conf_bins[bin_key][1] += 1
                    if knn5c == true_nc:
                        conf_bins[bin_key][0] += 1

        for m in METHODS:
            totals[m][0] += d[m][0]
            totals[m][1] += d[m][1]

        if n <= 15:
            bk = '5-15'
        elif n <= 50:
            bk = '16-50'
        elif n <= 100:
            bk = '51-100'
        else:
            bk = '101+'
        for m in METHODS:
            buckets[bk][m][0] += d[m][0]
            buckets[bk][m][1] += d[m][1]

        per_district.append({
            'leaid': leaid, 'n_schools': len(schools), 'n_samples': n,
            **{m: d[m][0] / d[m][1] if d[m][1] else 0 for m in METHODS},
        })

    print(f"\n{'='*76}")
    print(f"CROSS-VALIDATION RESULTS ({args.folds}-fold)")
    print(f"{'='*76}")
    print(f"\n{'Method':<25} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"{'-'*25} {'-'*8} {'-'*8} {'-'*10}")
    for m in METHODS:
        ok, tot = totals[m]
        label = {'voronoi': 'Voronoi (nearest sch)',
                 '1-nn': '1-NN (nearest sample)',
                 '5-nn': '5-NN (majority vote)',
                 '5-nn-conf': '5-NN + confidence'}[m]
        if tot:
            print(f"{label:<25} {ok:>8} {tot:>8} {ok/tot*100:>9.1f}%")

    print(f"\nBy district sample density:")
    header = f"{'Bucket':<12}"
    for m in METHODS:
        header += f" {m:>10}"
    header += f" {'Districts':>10}"
    print(header)
    print(f"{'-'*12}" + f" {'-'*10}" * (len(METHODS) + 1))
    for bk in ('5-15', '16-50', '51-100', '101+'):
        row = f"{bk:<12}"
        for m in METHODS:
            ok, tot = buckets[bk][m]
            row += f" {ok/tot*100:.1f}%".rjust(11) if tot else "          —"
        n_dist = sum(1 for d in per_district
                     if (bk == '5-15' and 5 <= d['n_samples'] <= 15) or
                        (bk == '16-50' and 16 <= d['n_samples'] <= 50) or
                        (bk == '51-100' and 51 <= d['n_samples'] <= 100) or
                        (bk == '101+' and d['n_samples'] > 100))
        row += f" {n_dist:>10}"
        print(row)

    # Confidence calibration: does the confidence score predict accuracy?
    print(f"\nConfidence calibration (5-NN + confidence):")
    print(f"{'Confidence':>12} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"{'-'*12} {'-'*8} {'-'*8} {'-'*10}")
    for bk in sorted(conf_bins.keys()):
        ok, tot = conf_bins[bk]
        if tot >= 10:
            print(f"{bk:>12} {ok:>8} {tot:>8} {ok/tot*100:>9.1f}%")

    # Best method per district
    best_method_counts = defaultdict(int)
    for d in per_district:
        best = max(METHODS, key=lambda m: d.get(m, 0))
        best_method_counts[best] += 1
    print(f"\nBest method per district:")
    for m, c in sorted(best_method_counts.items(), key=lambda x: -x[1]):
        print(f"  {m}: {c} districts")

    conn.close()


if __name__ == '__main__':
    sys.exit(main())
