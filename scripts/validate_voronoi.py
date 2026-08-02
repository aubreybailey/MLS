#!/usr/bin/env python3
"""
Validate enrollment-weighted Voronoi tessellation against SABS ground truth.

For each district that has SABS attendance zones AND multiple elementary schools,
generate a Voronoi partition of the district boundary weighted by enrollment,
then sample random points and compare the Voronoi assignment to the SABS
assignment. This tells us how often the cheapest geometric guess gets the
school right — and where it fails.

Usage:
    python scripts/validate_voronoi.py
    python scripts/validate_voronoi.py --samples 500 --level middle
"""

import argparse
import math
import os
import sqlite3
import sys
import time

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, MultiPoint
from shapely.ops import voronoi_diagram

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db


def _load_sabs_zones(state, level='elementary'):
    """Load SABS attendance zones as a GeoDataFrame with leaid+ncessch."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data', 'attendance_zones.gpkg')
    zones = gpd.read_file(path, layer=state)
    zones = zones[zones['ncessch'].notna()].copy()
    return zones


def _load_district_boundaries(state):
    """Load district boundaries from the merged GeoPackage.
    Layers: unsd (unified), elsd (elementary), scsd (secondary)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data', 'school_districts.gpkg')
    frames = []
    for layer in ('unsd', 'elsd', 'scsd'):
        try:
            df = gpd.read_file(path, layer=layer)
            df = df[df['STATE_FIPS'] == '25'].copy()
            frames.append(df)
        except Exception:
            pass
    districts = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True))
    districts['leaid'] = districts['GEOID']
    return districts


def _schools_for_district(conn, leaid, level='elementary'):
    """Get school locations and enrollment for a district+level."""
    rows = conn.execute(
        'SELECT s.ncessch, s.name, s.lat, s.lon, s.enrollment '
        'FROM schools s '
        'LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
        'WHERE s.leaid = ? AND s.level = ? '
        "AND COALESCE(r.source, '') NOT IN ('not-rated-alt', 'not-rated-pk') "
        'AND s.lat IS NOT NULL AND s.lon IS NOT NULL',
        (leaid, level)).fetchall()
    return rows


def _weighted_voronoi_assign(point, school_locs, school_enrollments):
    """Assign a point to the nearest school, weighted by enrollment.

    Weighting: distance is divided by sqrt(enrollment), so larger schools
    pull from a wider area. This is a power-diagram approximation."""
    best_nc = None
    best_score = float('inf')
    px, py = point.x, point.y
    for i, (nc, lat, lon, enroll) in enumerate(school_locs):
        dx = (px - lon) * math.cos(math.radians(lat)) * 69.0
        dy = (py - lat) * 69.0
        dist = math.sqrt(dx * dx + dy * dy)
        weight = math.sqrt(max(enroll, 1))
        score = dist / weight
        if score < best_score:
            best_score = score
            best_nc = nc
    return best_nc


def _nearest_assign(point, school_locs):
    """Assign a point to the nearest school (unweighted)."""
    best_nc = None
    best_dist = float('inf')
    px, py = point.x, point.y
    for nc, lat, lon, enroll in school_locs:
        dx = (px - lon) * math.cos(math.radians(lat)) * 69.0
        dy = (py - lat) * 69.0
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < best_dist:
            best_dist = dist
            best_nc = nc
    return best_nc


def _sample_points_in_polygon(polygon, n):
    """Generate n random points within a polygon."""
    minx, miny, maxx, maxy = polygon.bounds
    points = []
    attempts = 0
    while len(points) < n and attempts < n * 50:
        x = np.random.uniform(minx, maxx)
        y = np.random.uniform(miny, maxy)
        p = Point(x, y)
        if polygon.contains(p):
            points.append(p)
        attempts += 1
    return points


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--level', default='elementary')
    ap.add_argument('--samples', type=int, default=200,
                    help='random points per district (default 200)')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    np.random.seed(args.seed)
    state = 'MA'

    print(f"Loading SABS zones for {state}...")
    zones_gdf = _load_sabs_zones(state)

    # Filter zones to only the requested level by joining with schools table
    conn = sqlite3.connect(db.DB_PATH)
    level_ncs = {r[0] for r in conn.execute(
        'SELECT ncessch FROM schools WHERE state = ? AND level = ?',
        (state, args.level)).fetchall()}
    zones_gdf = zones_gdf[zones_gdf['ncessch'].isin(level_ncs)].copy()
    print(f"  {len(zones_gdf)} zones at {args.level} level")

    sabs_leaids = set(zones_gdf['leaid'].unique())

    print(f"Loading district boundaries...")
    districts_gdf = _load_district_boundaries(state)
    dist_geom = {r['leaid']: r['geometry']
                 for _, r in districts_gdf.iterrows()
                 if r['leaid'] in sabs_leaids}

    # Find districts with SABS zones AND multiple schools
    eligible = []
    for leaid in sorted(sabs_leaids):
        schools = _schools_for_district(conn, leaid, args.level)
        if len(schools) < 2:
            continue
        if leaid not in dist_geom:
            continue
        # Check that SABS has zones for multiple schools in this district
        zone_schools = set(zones_gdf[zones_gdf['leaid'] == leaid]['ncessch'])
        if len(zone_schools) < 2:
            continue
        eligible.append((leaid, schools, zone_schools))

    print(f"\nEligible districts (SABS + {len(eligible)} multi-school): {len(eligible)}")
    print(f"Sampling {args.samples} points per district...\n")

    # Prepare SABS spatial index for point-in-polygon lookups
    zones_gdf = zones_gdf.set_index('ncessch', drop=False)
    sabs_sindex = zones_gdf.sindex

    results = {
        'nearest': {'correct': 0, 'total': 0},
        'weighted': {'correct': 0, 'total': 0},
    }
    per_district = []

    t0 = time.time()
    for leaid, schools, zone_schools in eligible:
        geom = dist_geom[leaid]
        points = _sample_points_in_polygon(geom, args.samples)
        if len(points) < 10:
            continue

        school_locs = [(s[0], s[2], s[3], s[4] or 100) for s in schools]
        dist_zones = zones_gdf[zones_gdf['leaid'] == leaid]

        d_nearest_ok = 0
        d_weighted_ok = 0
        d_total = 0

        for pt in points:
            # Ground truth: which SABS zone contains this point?
            candidates = dist_zones[dist_zones.contains(pt)]
            if len(candidates) == 0:
                continue
            true_nc = candidates.iloc[0]['ncessch']

            nearest_nc = _nearest_assign(pt, school_locs)
            weighted_nc = _weighted_voronoi_assign(pt, school_locs, None)

            d_total += 1
            if nearest_nc == true_nc:
                d_nearest_ok += 1
                results['nearest']['correct'] += 1
            if weighted_nc == true_nc:
                d_weighted_ok += 1
                results['weighted']['correct'] += 1
            results['nearest']['total'] += 1
            results['weighted']['total'] += 1

        if d_total > 0:
            n_schools = len(schools)
            baseline = 1.0 / n_schools
            per_district.append({
                'leaid': leaid,
                'name': schools[0][1].split()[0],
                'n_schools': n_schools,
                'n_points': d_total,
                'nearest_acc': d_nearest_ok / d_total,
                'weighted_acc': d_weighted_ok / d_total,
                'baseline': baseline,
            })

    elapsed = time.time() - t0
    print(f"Validated {len(per_district)} districts in {elapsed:.0f}s\n")

    # Overall results
    for method in ('nearest', 'weighted'):
        r = results[method]
        acc = r['correct'] / r['total'] if r['total'] else 0
        print(f"{method:>10}: {r['correct']}/{r['total']} = {acc:.1%} correct")

    # Random baseline
    baselines = [d['baseline'] for d in per_district]
    avg_baseline = np.mean(baselines) if baselines else 0
    print(f"{'baseline':>10}: {avg_baseline:.1%} (random assignment)")

    # Per-school-count breakdown
    print(f"\n{'Schools':>7} {'Districts':>9} {'Nearest':>10} {'Weighted':>10} {'Baseline':>10}")
    print("-" * 52)
    for n in sorted(set(d['n_schools'] for d in per_district)):
        subset = [d for d in per_district if d['n_schools'] == n]
        if not subset:
            continue
        avg_n = np.mean([d['nearest_acc'] for d in subset])
        avg_w = np.mean([d['weighted_acc'] for d in subset])
        avg_b = np.mean([d['baseline'] for d in subset])
        print(f"{n:>7} {len(subset):>9} {avg_n:>10.1%} {avg_w:>10.1%} {avg_b:>10.1%}")

    # Worst districts
    per_district.sort(key=lambda d: d['weighted_acc'])
    print(f"\nWorst 10 districts (weighted Voronoi):")
    print(f"{'District':<25} {'Schools':>7} {'Nearest':>10} {'Weighted':>10}")
    print("-" * 57)
    for d in per_district[:10]:
        name = conn.execute("SELECT name FROM districts WHERE leaid = ?",
                            (d['leaid'],)).fetchone()
        dname = name[0] if name else d['leaid']
        print(f"{dname:<25} {d['n_schools']:>7} {d['nearest_acc']:>10.1%} {d['weighted_acc']:>10.1%}")

    # Best districts
    per_district.sort(key=lambda d: -d['weighted_acc'])
    print(f"\nBest 10 districts (weighted Voronoi):")
    print(f"{'District':<25} {'Schools':>7} {'Nearest':>10} {'Weighted':>10}")
    print("-" * 57)
    for d in per_district[:10]:
        name = conn.execute("SELECT name FROM districts WHERE leaid = ?",
                            (d['leaid'],)).fetchone()
        dname = name[0] if name else d['leaid']
        print(f"{dname:<25} {d['n_schools']:>7} {d['nearest_acc']:>10.1%} {d['weighted_acc']:>10.1%}")

    # Per-point rating impact: when Voronoi gets the wrong school,
    # does the rating shown to the user actually change?
    print(f"\n=== Per-point rating impact ===")
    print("When Voronoi assigns the wrong school, does the rating differ?\n")

    # Build ncessch -> rating lookup
    all_ratings = {}
    for d in per_district:
        for nc, name, lat, lon, enroll in _schools_for_district(conn, d['leaid'], args.level):
            if nc not in all_ratings:
                row = conn.execute(
                    'SELECT rating FROM school_ratings WHERE ncessch = ?', (nc,)).fetchone()
                all_ratings[nc] = row[0] if row else None

    # Re-run point sampling with rating comparison
    rating_same = 0
    rating_diff = 0
    rating_deltas = []
    total_with_ratings = 0

    for leaid, schools, zone_schools in eligible:
        geom = dist_geom[leaid]
        np.random.seed(args.seed + hash(leaid) % 10000)
        points = _sample_points_in_polygon(geom, args.samples)
        school_locs = [(s[0], s[2], s[3], s[4] or 100) for s in schools]
        dist_zones = zones_gdf[zones_gdf['leaid'] == leaid]

        for pt in points:
            candidates = dist_zones[dist_zones.contains(pt)]
            if len(candidates) == 0:
                continue
            true_nc = candidates.iloc[0]['ncessch']
            voronoi_nc = _weighted_voronoi_assign(pt, school_locs, None)

            true_r = all_ratings.get(true_nc)
            vor_r = all_ratings.get(voronoi_nc)
            if true_r is None or vor_r is None:
                continue
            total_with_ratings += 1
            if true_nc == voronoi_nc or true_r == vor_r:
                rating_same += 1
            else:
                rating_diff += 1
                rating_deltas.append(vor_r - true_r)

    print(f"Points with both ratings:  {total_with_ratings}")
    print(f"Rating matches (correct or same rating): {rating_same} ({rating_same/total_with_ratings:.1%})")
    print(f"Rating differs:            {rating_diff} ({rating_diff/total_with_ratings:.1%})")
    if rating_deltas:
        deltas = np.array(rating_deltas)
        print(f"  Mean delta (Voronoi - true): {deltas.mean():+.1f}")
        print(f"  Median delta:                {np.median(deltas):+.0f}")
        print(f"  |delta| = 1:                 {np.sum(np.abs(deltas) == 1)}")
        print(f"  |delta| >= 2:                {np.sum(np.abs(deltas) >= 2)}")
        print(f"  |delta| >= 3:                {np.sum(np.abs(deltas) >= 3)}")

    print(f"\nNote: district-min floor is computed from ALL schools in the district,")
    print(f"not the assigned one. Voronoi errors affect the specific school shown")
    print(f"in the 'zoned' column, but the floor rating is always the true worst case.")


if __name__ == '__main__':
    sys.exit(main() or 0)
