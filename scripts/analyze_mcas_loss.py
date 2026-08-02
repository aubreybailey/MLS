#!/usr/bin/env python3
"""
Quantify what the app loses by using MCAS-derived ratings instead of
GreatSchools.

Reads from cache/schools.db (must be built first via setup_state.py --state MA)
and fetches MCAS proficiency data from the DESE portal.  Produces a summary on
stdout.

The key metric is not per-school MAE — it's how often the **district-min floor**
changes.  The floor drives --min-rating filtering in search.py, so an optimistic
floor (MCAS says 7, GS says 5) lets through listings the user wanted excluded.

Usage:
    python scripts/analyze_mcas_loss.py --state MA
    python scripts/analyze_mcas_loss.py --state MA --output docs/mcas-loss-report.md
"""

import argparse
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from backfill_mcas import build_crosswalk, fetch_mcas, proficiency_to_rating

import numpy as np


def load_paired_schools(conn, crosswalk, mcas, state):
    """Schools with BOTH a GS rating AND MCAS proficiency data.

    Returns list of dicts with gs_rating, mcas_proficiency, mcas_rating, delta."""
    gs_rows = conn.execute(
        'SELECT s.ncessch, s.name, s.leaid, s.level, r.rating '
        'FROM schools s JOIN school_ratings r ON r.ncessch = s.ncessch '
        "WHERE s.state = ? AND r.source = 'greatschools' AND r.rating IS NOT NULL",
        (state,)).fetchall()

    pairs = []
    for ncessch, name, leaid, level, gs_rating in gs_rows:
        dese = crosswalk.get(ncessch)
        if not dese or dese not in mcas:
            continue
        prof = mcas[dese]['proficiency']
        mcas_rating = proficiency_to_rating(prof)
        pairs.append({
            'ncessch': ncessch,
            'name': name,
            'leaid': leaid,
            'level': level,
            'gs_rating': gs_rating,
            'mcas_proficiency': prof,
            'mcas_rating': mcas_rating,
            'delta': mcas_rating - gs_rating,
        })
    return pairs


def per_school_metrics(pairs):
    """Aggregate accuracy metrics across all paired schools."""
    deltas = np.array([p['delta'] for p in pairs])
    abs_deltas = np.abs(deltas)
    return {
        'n': len(pairs),
        'mae': float(np.mean(abs_deltas)),
        'rmse': float(np.sqrt(np.mean(deltas ** 2))),
        'exact_match_pct': float(np.mean(abs_deltas == 0)),
        'within_1_pct': float(np.mean(abs_deltas <= 1)),
        'within_2_pct': float(np.mean(abs_deltas <= 2)),
        'mean_delta': float(np.mean(deltas)),
        'median_delta': float(np.median(deltas)),
    }


def per_tier_breakdown(pairs):
    """For each GS rating tier (1-10), compute accuracy metrics."""
    tiers = {}
    for p in pairs:
        gs = p['gs_rating']
        if gs not in tiers:
            tiers[gs] = []
        tiers[gs].append(p['delta'])

    rows = []
    for gs_tier in sorted(tiers):
        deltas = np.array(tiers[gs_tier])
        abs_d = np.abs(deltas)
        rows.append({
            'gs_tier': gs_tier,
            'n': len(deltas),
            'mae': float(np.mean(abs_d)),
            'mean_delta': float(np.mean(deltas)),
            'exact_match_pct': float(np.mean(abs_d == 0)),
            'within_1_pct': float(np.mean(abs_d <= 1)),
        })
    return rows


def district_floor_impact(pairs, conn, state):
    """For each district with ≥2 schools in the overlap set, compare
    district-min under GS vs MCAS ratings.

    Returns aggregate metrics and a list of per-district results."""
    # Build per-district lookup
    by_district = {}
    for p in pairs:
        by_district.setdefault((p['leaid'], p['level']), []).append(p)

    per_district = []
    for (leaid, level), schools in by_district.items():
        if len(schools) < 2:
            continue

        gs_min = min(s['gs_rating'] for s in schools)
        mcas_min = min(s['mcas_rating'] for s in schools)
        gs_max = max(s['gs_rating'] for s in schools)
        mcas_max = max(s['mcas_rating'] for s in schools)
        delta = mcas_min - gs_min

        # Get district name
        dname = conn.execute(
            'SELECT name FROM districts WHERE leaid = ?', (leaid,)).fetchone()
        # districts table has school names, try to get a better name
        if not dname:
            dname = (leaid,)

        per_district.append({
            'leaid': leaid,
            'level': level,
            'name': dname[0],
            'n_schools': len(schools),
            'gs_min': gs_min,
            'mcas_min': mcas_min,
            'floor_delta': delta,
            'gs_range': f"{gs_min}-{gs_max}",
            'mcas_range': f"{mcas_min}-{mcas_max}",
        })

    floor_deltas = np.array([d['floor_delta'] for d in per_district])
    abs_deltas = np.abs(floor_deltas)

    agg = {
        'n_districts': len(per_district),
        'floor_same_pct': float(np.mean(floor_deltas == 0)),
        'floor_within_1_pct': float(np.mean(abs_deltas <= 1)),
        'floor_higher_count': int(np.sum(floor_deltas > 0)),
        'floor_higher_by_2plus': int(np.sum(floor_deltas >= 2)),
        'floor_lower_count': int(np.sum(floor_deltas < 0)),
        'floor_mae': float(np.mean(abs_deltas)),
        'mean_floor_delta': float(np.mean(floor_deltas)),
    }

    return agg, per_district


def bootstrap_ci(values, metric_fn, n_boot=10000, ci=0.95):
    """Bootstrap confidence interval for a scalar metric."""
    rng = np.random.default_rng(42)
    stats = []
    for _ in range(n_boot):
        sample = rng.choice(values, size=len(values), replace=True)
        stats.append(metric_fn(sample))
    lower = np.percentile(stats, (1 - ci) / 2 * 100)
    upper = np.percentile(stats, (1 + ci) / 2 * 100)
    return float(lower), float(upper)


def coverage_analysis(conn, crosswalk, mcas, state):
    """How many schools get rated from MCAS alone?"""
    all_schools = conn.execute(
        'SELECT s.ncessch, s.name, s.level, s.grade_hi, s.enrollment '
        'FROM schools s WHERE s.state = ?', (state,)).fetchall()

    # Get existing tags
    tags = {}
    for nc, src in conn.execute(
            'SELECT ncessch, source FROM school_ratings '
            'WHERE ncessch IN (SELECT ncessch FROM schools WHERE state = ?)',
            (state,)).fetchall():
        tags[nc] = src

    total = len(all_schools)
    by_level = {}
    mcas_rated = 0
    no_mcas_reasons = {'pk_only': 0, 'alt_program': 0, 'no_crosswalk': 0,
                       'no_test_data': 0}

    for ncessch, name, level, grade_hi, enrollment in all_schools:
        by_level[level] = by_level.get(level, 0) + 1
        dese = crosswalk.get(ncessch)
        if dese and dese in mcas:
            mcas_rated += 1
        else:
            tag = tags.get(ncessch, '')
            if tag == 'not-rated-pk':
                no_mcas_reasons['pk_only'] += 1
            elif tag == 'not-rated-alt':
                no_mcas_reasons['alt_program'] += 1
            elif not dese:
                no_mcas_reasons['no_crosswalk'] += 1
            else:
                no_mcas_reasons['no_test_data'] += 1

    ratable = total - no_mcas_reasons['pk_only'] - no_mcas_reasons['alt_program']
    return {
        'total': total,
        'by_level': by_level,
        'mcas_rated': mcas_rated,
        'no_mcas_reasons': no_mcas_reasons,
        'coverage_pct': mcas_rated / ratable if ratable else 0,
        'ratable': ratable,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--ccd-year', type=int, default=2022)
    ap.add_argument('--mcas-year', type=int, default=2025)
    ap.add_argument('--output', help='write Markdown report to file')
    args = ap.parse_args()
    state = args.state.upper()

    conn = sqlite3.connect(db.DB_PATH)

    print(f"Building NCES → DESE crosswalk ({args.ccd_year})...")
    crosswalk = build_crosswalk(state, args.ccd_year)
    print(f"  {len(crosswalk)} schools mapped")

    print(f"Fetching MCAS {args.mcas_year} results...")
    mcas = fetch_mcas(args.mcas_year)
    print(f"  {len(mcas)} schools with data\n")

    # --- Paired schools ---
    pairs = load_paired_schools(conn, crosswalk, mcas, state)
    print(f"{'=' * 62}")
    print(f"PAIRED SCHOOLS: {len(pairs)} with both GS rating and MCAS data")
    print(f"{'=' * 62}\n")

    m = per_school_metrics(pairs)
    print(f"  MAE:            {m['mae']:.2f}")
    print(f"  RMSE:           {m['rmse']:.2f}")
    print(f"  Exact match:    {m['exact_match_pct']:.1%}")
    print(f"  Within ±1:      {m['within_1_pct']:.1%}")
    print(f"  Within ±2:      {m['within_2_pct']:.1%}")
    print(f"  Mean delta:     {m['mean_delta']:+.2f} (positive = MCAS rates higher)")
    print(f"  Median delta:   {m['median_delta']:+.0f}")

    # Bootstrap CI on MAE
    deltas = np.array([p['delta'] for p in pairs])
    ci_lo, ci_hi = bootstrap_ci(
        deltas, lambda d: float(np.mean(np.abs(d))))
    print(f"  MAE 95% CI:     [{ci_lo:.2f}, {ci_hi:.2f}]")

    # --- Per-tier breakdown ---
    print(f"\n{'GS':>4} {'N':>6} {'MAE':>6} {'Mean Δ':>8} {'Exact':>7} {'±1':>7}")
    print("-" * 44)
    for t in per_tier_breakdown(pairs):
        print(f"{t['gs_tier']:>4} {t['n']:>6} {t['mae']:>6.2f} "
              f"{t['mean_delta']:>+8.2f} {t['exact_match_pct']:>7.1%} "
              f"{t['within_1_pct']:>7.1%}")

    # --- District floor impact ---
    print(f"\n{'=' * 62}")
    print("DISTRICT-MIN FLOOR IMPACT (the metric that matters)")
    print(f"{'=' * 62}\n")

    for level in ('elementary', 'middle', 'high'):
        level_pairs = [p for p in pairs if p['level'] == level]
        if len(level_pairs) < 10:
            print(f"  {level}: too few paired schools ({len(level_pairs)})")
            continue

        agg, per_dist = district_floor_impact(level_pairs, conn, state)
        print(f"  {level.upper()} ({agg['n_districts']} districts with ≥2 overlap schools):")
        print(f"    Floor unchanged:       {agg['floor_same_pct']:.1%}")
        print(f"    Floor within ±1:       {agg['floor_within_1_pct']:.1%}")
        print(f"    Floor MAE:             {agg['floor_mae']:.2f}")
        print(f"    Mean floor delta:      {agg['mean_floor_delta']:+.2f}")
        print(f"    Floor HIGHER (danger): {agg['floor_higher_count']} districts")
        print(f"    Floor higher by ≥2:    {agg['floor_higher_by_2plus']} districts  *** DANGER ***")
        print(f"    Floor LOWER (safe):    {agg['floor_lower_count']} districts")

        # Show the danger districts
        danger = [d for d in per_dist if d['floor_delta'] >= 2]
        if danger:
            danger.sort(key=lambda d: -d['floor_delta'])
            print(f"\n    Danger districts (MCAS floor ≥2 higher than GS):")
            print(f"    {'District':<30} {'Schools':>7} {'GS floor':>9} {'MCAS floor':>10} {'Δ':>4}")
            print(f"    {'-'*64}")
            for d in danger:
                print(f"    {d['name']:<30} {d['n_schools']:>7} "
                      f"{d['gs_min']:>9} {d['mcas_min']:>10} {d['floor_delta']:>+4}")

        # Bootstrap CI on floor-change rate
        floor_deltas = np.array([d['floor_delta'] for d in per_dist])
        ci_lo, ci_hi = bootstrap_ci(
            floor_deltas, lambda d: float(np.mean(d >= 2)))
        print(f"\n    Floor-higher-by-≥2 rate 95% CI: [{ci_lo:.1%}, {ci_hi:.1%}]")
        print()

    # --- Coverage ---
    print(f"{'=' * 62}")
    print("COVERAGE (MCAS alone, no GS)")
    print(f"{'=' * 62}\n")

    cov = coverage_analysis(conn, crosswalk, mcas, state)
    print(f"  Total schools:  {cov['total']}")
    print(f"  MCAS can rate:  {cov['mcas_rated']}")
    print(f"  Coverage:       {cov['coverage_pct']:.1%} of ratable schools "
          f"({cov['mcas_rated']}/{cov['ratable']})")
    print(f"\n  Schools without MCAS data:")
    for reason, count in cov['no_mcas_reasons'].items():
        if count:
            print(f"    {reason:<20} {count:>5}")

    # --- Write report file ---
    if args.output:
        lines = [
            f"# MCAS vs GreatSchools Loss Analysis ({state})\n",
            f"Generated from {len(pairs)} schools with both ratings.\n",
            f"## Per-school accuracy\n",
            f"- MAE: {m['mae']:.2f} (95% CI: [{ci_lo:.2f}, {ci_hi:.2f}])",
            f"- Exact match: {m['exact_match_pct']:.1%}",
            f"- Within ±1: {m['within_1_pct']:.1%}",
            f"- Mean delta: {m['mean_delta']:+.2f}\n",
        ]
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, 'w') as f:
            f.write('\n'.join(lines))
        print(f"\nReport written to {args.output}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
