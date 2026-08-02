#!/usr/bin/env python3
"""
Validate zone inference methods against SABS ground truth.

Compares three classifiers on random points in SABS-covered districts:
  1. Voronoi (nearest school) — geometric baseline
  2. Pure NN (nearest labeled sample) — what we had before
  3. Bayesian (Voronoi prior + sample evidence) — new approach

For each, reports accuracy overall and broken down by sample density.

Usage:
    python scripts/validate_mls_zones.py --state MA
    python scripts/validate_mls_zones.py --state MA --samples 300
"""

import argparse
import math
import os
import random
import sqlite3
import sys

import geopandas as gpd
from shapely.geometry import Point

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_sabs_zones(state, level='elementary'):
    path = os.path.join(ROOT, 'data', 'attendance_zones.gpkg')
    zones = gpd.read_file(path, layer=state)
    zones = zones[zones['ncessch'].notna()].copy()
    conn = sqlite3.connect(db.DB_PATH)
    level_ncs = {r[0] for r in conn.execute(
        'SELECT ncessch FROM schools WHERE state = ? AND level = ?',
        (state, level)).fetchall()}
    conn.close()
    zones = zones[zones['ncessch'].isin(level_ncs)].copy()
    return zones


def _load_district_boundaries():
    path = os.path.join(ROOT, 'data', 'school_districts.gpkg')
    frames = []
    for layer in ('unsd', 'elsd', 'scsd'):
        try:
            df = gpd.read_file(path, layer=layer)
            df = df[df['STATE_FIPS'] == '25'].copy()
            frames.append(df)
        except Exception:
            pass
    districts = gpd.GeoDataFrame(gpd.pd.concat(frames, ignore_index=True))
    districts['leaid'] = districts['GEOID']
    return districts


def _sample_points_in_polygon(polygon, n):
    pts = []
    minx, miny, maxx, maxy = polygon.bounds
    attempts = 0
    while len(pts) < n and attempts < n * 50:
        x = random.uniform(minx, maxx)
        y = random.uniform(miny, maxy)
        p = Point(x, y)
        if polygon.contains(p):
            pts.append(p)
        attempts += 1
    return pts


def _haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _nearest_school(lat, lon, school_locs):
    """Pure Voronoi: assign to nearest school."""
    best_nc = None
    best_dist = float('inf')
    for nc, slat, slon, _ in school_locs:
        d = _haversine(lat, lon, slat, slon)
        if d < best_dist:
            best_dist = d
            best_nc = nc
    return best_nc


def _nearest_sample(lat, lon, level, conn, max_miles=0.6):
    """Pure NN: assign to nearest labeled sample (old method)."""
    dlat = max_miles / 69.0
    dlon = max_miles / max(0.1, 69.0 * math.cos(math.radians(lat)))
    rows = conn.execute(
        'SELECT lat, lon, ncessch FROM zone_samples '
        'WHERE ncessch IS NOT NULL AND level = ? '
        'AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
        (level, lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
    best_nc = None
    best_d = max_miles
    for slat, slon, snc in rows:
        d = _haversine(lat, lon, slat, slon)
        if d <= best_d:
            best_d = d
            best_nc = snc
    return best_nc


def _bayesian_classify(lat, lon, level, school_locs, conn,
                       sigma=1.5, lam=0.4, alpha=0.5, radius=1.0):
    """Bayesian: Voronoi prior + sample evidence."""
    dlat = radius / 69.0
    dlon = radius / max(0.1, 69.0 * math.cos(math.radians(lat)))
    samples = conn.execute(
        'SELECT lat, lon, ncessch FROM zone_samples '
        'WHERE ncessch IS NOT NULL AND level = ? '
        'AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
        (level, lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()

    candidate_ncs = {s[0] for s in school_locs}

    # Prior: softmax over distance to schools
    school_dists = {nc: _haversine(lat, lon, slat, slon)
                    for nc, slat, slon, _ in school_locs}
    log_prior = {nc: -d / sigma for nc, d in school_dists.items()}
    max_lp = max(log_prior.values())
    exp_prior = {nc: math.exp(lp - max_lp) for nc, lp in log_prior.items()}
    pt = sum(exp_prior.values())
    prior = {nc: e / pt for nc, e in exp_prior.items()}

    # Evidence: distance-weighted votes
    votes = {nc: 0.0 for nc in candidate_ncs}
    for slat, slon, snc in samples:
        if snc not in candidate_ncs:
            continue
        d = _haversine(lat, lon, slat, slon)
        if d <= radius:
            votes[snc] += math.exp(-d / lam)

    K = len(candidate_ncs)
    ev_total = sum(votes.values()) + alpha * K
    evidence = {nc: (votes[nc] + alpha) / ev_total for nc in candidate_ncs}

    # Posterior
    raw = {nc: prior[nc] * evidence[nc] for nc in candidate_ncs}
    post_total = sum(raw.values())
    if post_total == 0:
        return _nearest_school(lat, lon, school_locs)
    posterior = {nc: r / post_total for nc, r in raw.items()}
    return max(posterior, key=posterior.get)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--level', default='elementary')
    ap.add_argument('--samples', type=int, default=200)
    ap.add_argument('--sigma', type=float, default=1.5)
    ap.add_argument('--lam', type=float, default=0.4)
    args = ap.parse_args()

    random.seed(42)

    zones_gdf = _load_sabs_zones(args.state, args.level)
    districts_gdf = _load_district_boundaries()
    conn = sqlite3.connect(db.DB_PATH)

    multi_school = zones_gdf.groupby('leaid').filter(
        lambda g: g['ncessch'].nunique() >= 2)
    district_ids = multi_school['leaid'].unique()
    print(f"SABS districts with ≥2 {args.level} schools: {len(district_ids)}")

    # Count MLS samples per district
    mls_counts = {}
    for leaid in district_ids:
        n = conn.execute(
            'SELECT COUNT(*) FROM zone_samples zs '
            'JOIN schools s ON zs.ncessch = s.ncessch '
            'WHERE s.leaid = ? AND s.level = ?',
            (leaid, args.level)).fetchone()[0]
        mls_counts[leaid] = n

    voronoi_ok = voronoi_tot = 0
    nn_ok = nn_tot = 0
    bayes_ok = bayes_tot = 0

    # Track by sample density bucket
    buckets = {'0': [0, 0, 0, 0, 0, 0],       # [vor_ok, vor_tot, nn_ok, nn_tot, bay_ok, bay_tot]
               '1-10': [0, 0, 0, 0, 0, 0],
               '11-30': [0, 0, 0, 0, 0, 0],
               '31+': [0, 0, 0, 0, 0, 0]}

    per_district = []

    for leaid in district_ids:
        dz = zones_gdf[zones_gdf['leaid'] == leaid]
        dist_row = districts_gdf[districts_gdf['leaid'] == leaid]
        if dist_row.empty:
            continue
        boundary = dist_row.iloc[0].geometry

        schools = conn.execute(
            'SELECT s.ncessch, s.lat, s.lon, s.enrollment FROM schools s '
            'LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
            'WHERE s.leaid = ? AND s.level = ? '
            "AND COALESCE(r.source, '') NOT IN ('not-rated-alt', 'not-rated-pk') "
            'AND s.lat IS NOT NULL AND s.lon IS NOT NULL',
            (leaid, args.level)).fetchall()
        if len(schools) < 2:
            continue

        school_locs = [(s[0], s[1], s[2], s[3] or 1) for s in schools]
        pts = _sample_points_in_polygon(boundary, args.samples)

        n_mls = mls_counts.get(leaid, 0)
        if n_mls == 0:
            bk = '0'
        elif n_mls <= 10:
            bk = '1-10'
        elif n_mls <= 30:
            bk = '11-30'
        else:
            bk = '31+'

        d_v = d_vt = d_n = d_nt = d_b = d_bt = 0

        for pt in pts:
            true_zones = dz[dz.geometry.contains(pt)]
            if true_zones.empty:
                continue
            true_nc = true_zones.iloc[0]['ncessch']

            # Voronoi
            vor = _nearest_school(pt.y, pt.x, school_locs)
            d_vt += 1
            if vor == true_nc:
                d_v += 1

            # Pure NN
            nn = _nearest_sample(pt.y, pt.x, args.level, conn)
            if nn is not None:
                d_nt += 1
                if nn == true_nc:
                    d_n += 1

            # Bayesian
            bay = _bayesian_classify(pt.y, pt.x, args.level, school_locs, conn,
                                     sigma=args.sigma, lam=args.lam)
            d_bt += 1
            if bay == true_nc:
                d_b += 1

        voronoi_ok += d_v; voronoi_tot += d_vt
        nn_ok += d_n; nn_tot += d_nt
        bayes_ok += d_b; bayes_tot += d_bt

        b = buckets[bk]
        b[0] += d_v; b[1] += d_vt; b[2] += d_n; b[3] += d_nt; b[4] += d_b; b[5] += d_bt

        per_district.append({
            'leaid': leaid, 'n_schools': len(schools), 'n_mls': n_mls,
            'voronoi': d_v / d_vt if d_vt else 0,
            'nn': d_n / d_nt if d_nt else None,
            'bayesian': d_b / d_bt if d_bt else 0,
        })

    conn.close()

    print(f"\n{'='*70}")
    print(f"RESULTS ({voronoi_tot} points across {len(per_district)} districts)")
    print(f"{'='*70}")
    print(f"\n{'Method':<25} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print(f"{'-'*25} {'-'*8} {'-'*8} {'-'*10}")
    print(f"{'Voronoi (nearest sch)':<25} {voronoi_ok:>8} {voronoi_tot:>8} "
          f"{voronoi_ok/voronoi_tot*100:>9.1f}%")
    if nn_tot:
        print(f"{'Pure NN (nearest sample)':<25} {nn_ok:>8} {nn_tot:>8} "
              f"{nn_ok/nn_tot*100:>9.1f}%")
    print(f"{'Bayesian (prior+evidence)':<25} {bayes_ok:>8} {bayes_tot:>8} "
          f"{bayes_ok/bayes_tot*100:>9.1f}%")

    print(f"\nBy sample density:")
    print(f"{'Bucket':<12} {'Voronoi':>10} {'NN':>10} {'Bayesian':>10} {'Districts':>10}")
    print(f"{'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for bk in ('0', '1-10', '11-30', '31+'):
        b = buckets[bk]
        v_pct = f"{b[0]/b[1]*100:.1f}%" if b[1] else "—"
        n_pct = f"{b[2]/b[3]*100:.1f}%" if b[3] else "—"
        bay_pct = f"{b[4]/b[5]*100:.1f}%" if b[5] else "—"
        n_dist = sum(1 for d in per_district
                     if (bk == '0' and d['n_mls'] == 0) or
                        (bk == '1-10' and 1 <= d['n_mls'] <= 10) or
                        (bk == '11-30' and 11 <= d['n_mls'] <= 30) or
                        (bk == '31+' and d['n_mls'] > 30))
        print(f"{bk + ' samples':<12} {v_pct:>10} {n_pct:>10} {bay_pct:>10} {n_dist:>10}")

    # Where does Bayesian beat/lose to Voronoi?
    better = [(d, d['bayesian'] - d['voronoi'])
              for d in per_district if d['bayesian'] > d['voronoi'] + 0.05]
    worse = [(d, d['voronoi'] - d['bayesian'])
             for d in per_district if d['voronoi'] > d['bayesian'] + 0.05]
    better.sort(key=lambda x: -x[1])
    worse.sort(key=lambda x: -x[1])

    if better:
        print(f"\nBayesian BEATS Voronoi ({len(better)} districts):")
        for d, delta in better[:10]:
            print(f"  {d['leaid']}: +{delta*100:.0f}pp "
                  f"(Bayes {d['bayesian']*100:.0f}% vs Vor {d['voronoi']*100:.0f}%, "
                  f"{d['n_mls']} samples, {d['n_schools']} schools)")

    if worse:
        print(f"\nBayesian LOSES to Voronoi ({len(worse)} districts):")
        for d, delta in worse[:10]:
            print(f"  {d['leaid']}: -{delta*100:.0f}pp "
                  f"(Bayes {d['bayesian']*100:.0f}% vs Vor {d['voronoi']*100:.0f}%, "
                  f"{d['n_mls']} samples, {d['n_schools']} schools)")


if __name__ == '__main__':
    sys.exit(main())
