#!/usr/bin/env python3
"""
Build inferred elementary attendance zone polygons from labeled sample points.

Method A (label-constrained Voronoi):
  1. For each district with labeled points, compute Voronoi from NCES school
     locations clipped to the district boundary.
  2. Score each cell by concordance with labeled points.
  3. Where SABS ground-truth exists, use that instead.

Outputs a GeoPackage with per-zone confidence metadata.

Usage:
    python scripts/build_inferred_zones.py --state MA
    python scripts/build_inferred_zones.py --state MA --min-points 3
"""

import argparse
import math
import os
import sqlite3
import sys

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, MultiPoint, Polygon, MultiPolygon
from shapely.ops import voronoi_diagram

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, 'data')
GPKG_PATH = os.path.join(DATA_DIR, 'school_districts.gpkg')
ZONES_PATH = os.path.join(DATA_DIR, 'attendance_zones.gpkg')
OUTPUT_PATH = os.path.join(DATA_DIR, 'inferred_attendance_zones.gpkg')


def _district_boundary(leaid, district_gdfs):
    """Get the boundary polygon for a district from the loaded GeoPackage layers."""
    for gdf in district_gdfs.values():
        match = gdf[gdf['GEOID'] == leaid]
        if not match.empty:
            return match.iloc[0].geometry
    return None


def _schools_for_district(conn, leaid, level='elementary'):
    """Get school locations for a district, excluding alt/PK schools."""
    rows = conn.execute(
        'SELECT s.ncessch, s.name, s.lat, s.lon, s.enrollment '
        'FROM schools s '
        'LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
        'WHERE s.leaid = ? AND s.level = ? '
        "AND COALESCE(r.source, '') NOT IN ('not-rated-alt', 'not-rated-pk') "
        'AND s.lat IS NOT NULL AND s.lon IS NOT NULL',
        (leaid, level)).fetchall()
    return rows


def _samples_for_district(conn, leaid, level='elementary'):
    """Get labeled zone sample points within a district."""
    rows = conn.execute(
        'SELECT zs.lat, zs.lon, zs.ncessch, zs.school_name, zs.source '
        'FROM zone_samples zs '
        'JOIN schools s ON zs.ncessch = s.ncessch '
        'WHERE s.leaid = ? AND s.level = ? '
        'AND zs.ncessch IS NOT NULL',
        (leaid, level)).fetchall()
    return rows


def _voronoi_cells(schools, district_poly):
    """Build Voronoi cells from school locations, clipped to district boundary.

    Returns list of (ncessch, polygon) pairs."""
    if len(schools) < 2:
        if schools:
            return [(schools[0][0], district_poly)]
        return []

    points = MultiPoint([(s[3], s[2]) for s in schools])  # lon, lat

    try:
        regions = voronoi_diagram(points, envelope=district_poly)
    except Exception:
        return []

    school_map = {(round(s[3], 10), round(s[2], 10)): s[0] for s in schools}

    cells = []
    for geom in regions.geoms:
        clipped = geom.intersection(district_poly)
        if clipped.is_empty:
            continue

        centroid = geom.centroid
        best_nc = None
        best_dist = float('inf')
        for (slon, slat), nc in school_map.items():
            d = (centroid.x - slon) ** 2 + (centroid.y - slat) ** 2
            if d < best_dist:
                best_dist = d
                best_nc = nc

        if best_nc:
            cells.append((best_nc, clipped))

    return cells


def _score_concordance(cell_poly, ncessch, samples):
    """What fraction of labeled points in this cell agree with the assignment?"""
    in_cell = [(s[0], s[1], s[2]) for s in samples
               if cell_poly.contains(Point(s[1], s[0]))]
    if not in_cell:
        return 0.0, 0
    agree = sum(1 for _, _, nc in in_cell if nc == ncessch)
    return agree / len(in_cell), len(in_cell)


def _sabs_zones_for_district(leaid, state, level='elementary'):
    """Load SABS ground-truth zones for a district, if available."""
    if not os.path.exists(ZONES_PATH):
        return None

    try:
        import pyogrio
        layers = [l[0] for l in pyogrio.list_layers(ZONES_PATH)]
    except Exception:
        return None

    if state not in layers:
        return None

    try:
        all_zones = gpd.read_file(ZONES_PATH, layer=state)
    except Exception:
        return None

    conn = sqlite3.connect(db.DB_PATH)
    level_ncs = {r[0] for r in conn.execute(
        'SELECT ncessch FROM schools WHERE leaid = ? AND level = ?',
        (leaid, level)).fetchall()}
    conn.close()

    district_zones = all_zones[
        (all_zones['leaid'] == leaid) &
        (all_zones['ncessch'].isin(level_ncs))
    ]
    if district_zones.empty:
        return None
    return district_zones


def build_zones(state, level='elementary', min_points=5, verbose=False):
    """Build inferred zone polygons for all districts in a state."""
    conn = sqlite3.connect(db.DB_PATH)

    districts = conn.execute(
        'SELECT DISTINCT s.leaid, COALESCE(d.name, s.city) '
        'FROM schools s LEFT JOIN districts d ON d.leaid = s.leaid '
        'WHERE s.state = ? AND s.level = ?',
        (state, level)).fetchall()
    print(f"Found {len(districts)} districts with {level} schools in {state}")

    if not os.path.exists(GPKG_PATH):
        print(f"District GeoPackage not found at {GPKG_PATH}")
        return None

    district_gdfs = {}
    for dtype in ('unsd', 'elsd', 'scsd'):
        try:
            district_gdfs[dtype] = gpd.read_file(GPKG_PATH, layer=dtype)
        except Exception:
            pass

    all_zones = []
    stats = {'sabs': 0, 'mls_inferred': 0, 'voronoi_prior': 0,
             'single_school': 0, 'skipped': 0}

    for leaid, dist_name in districts:
        schools = _schools_for_district(conn, leaid, level)
        if not schools:
            stats['skipped'] += 1
            continue

        boundary = _district_boundary(leaid, district_gdfs)
        if boundary is None:
            stats['skipped'] += 1
            if verbose:
                print(f"  {dist_name} ({leaid}): no boundary found")
            continue

        if len(schools) == 1:
            nc, name, lat, lon, enroll = schools[0]
            all_zones.append({
                'ncessch': nc,
                'school_name': name,
                'leaid': leaid,
                'district_name': dist_name,
                'sample_count': 0,
                'concordance': 1.0,
                'method': 'single-school',
                'confidence': 'high',
                'geometry': boundary,
            })
            stats['single_school'] += 1
            continue

        sabs = _sabs_zones_for_district(leaid, state, level)
        if sabs is not None and len(sabs) >= len(schools) * 0.5:
            for _, row in sabs.iterrows():
                all_zones.append({
                    'ncessch': row.get('ncessch', ''),
                    'school_name': row.get('schnam', ''),
                    'leaid': leaid,
                    'district_name': dist_name,
                    'sample_count': 0,
                    'concordance': 1.0,
                    'method': 'sabs',
                    'confidence': 'high',
                    'geometry': row.geometry,
                })
            stats['sabs'] += 1
            if verbose:
                print(f"  {dist_name}: SABS ground truth ({len(sabs)} zones)")
            continue

        samples = _samples_for_district(conn, leaid, level)
        cells = _voronoi_cells(schools, boundary)

        if not cells:
            stats['skipped'] += 1
            continue

        school_names = {s[0]: s[1] for s in schools}

        has_enough_samples = len(samples) >= min_points
        for nc, poly in cells:
            concordance, n_in_cell = _score_concordance(poly, nc, samples)

            if has_enough_samples and n_in_cell >= 2:
                method = 'voronoi-mls'
                if n_in_cell >= 20 and concordance >= 0.7:
                    confidence = 'high'
                elif n_in_cell >= 5 and concordance >= 0.5:
                    confidence = 'medium'
                else:
                    confidence = 'low'
            else:
                method = 'voronoi-prior'
                confidence = 'low'

            all_zones.append({
                'ncessch': nc,
                'school_name': school_names.get(nc, ''),
                'leaid': leaid,
                'district_name': dist_name,
                'sample_count': n_in_cell,
                'concordance': round(concordance, 3),
                'method': method,
                'confidence': confidence,
                'geometry': poly,
            })

        if has_enough_samples:
            stats['mls_inferred'] += 1
            if verbose:
                print(f"  {dist_name}: MLS-inferred ({len(samples)} samples, "
                      f"{len(cells)} cells)")
        else:
            stats['voronoi_prior'] += 1
            if verbose:
                print(f"  {dist_name}: Voronoi prior ({len(samples)} samples)")

    conn.close()

    if not all_zones:
        print("No zones built")
        return None

    gdf = gpd.GeoDataFrame(all_zones, crs='EPSG:4326')

    print(f"\nBuilt {len(gdf)} zone polygons across {len(districts)} districts")
    print(f"  SABS ground truth: {stats['sabs']} districts")
    print(f"  MLS-inferred: {stats['mls_inferred']} districts")
    print(f"  Voronoi prior: {stats['voronoi_prior']} districts")
    print(f"  Single-school: {stats['single_school']} districts")
    print(f"  Skipped: {stats['skipped']} districts")
    print(f"\nConfidence breakdown:")
    for conf in ('high', 'medium', 'low'):
        n = len(gdf[gdf['confidence'] == conf])
        print(f"  {conf}: {n} zones")

    return gdf


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--level', default='elementary')
    ap.add_argument('--min-points', type=int, default=5,
                    help='minimum labeled points per district for MLS-inferred (default 5)')
    ap.add_argument('--verbose', '-v', action='store_true')
    ap.add_argument('--dry-run', action='store_true',
                    help='build and report but do not write GeoPackage')
    args = ap.parse_args()

    gdf = build_zones(args.state, args.level, args.min_points, args.verbose)
    if gdf is None:
        return 1

    if args.dry_run:
        print(f"\nDry run — would write {len(gdf)} zones to {OUTPUT_PATH}")
        return 0

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    layer = f"zones_{args.level}"
    gdf.to_file(OUTPUT_PATH, layer=layer, driver='GPKG')
    print(f"\nWrote {len(gdf)} zones to {OUTPUT_PATH} (layer: {layer})")

    return 0


if __name__ == '__main__':
    sys.exit(main())
