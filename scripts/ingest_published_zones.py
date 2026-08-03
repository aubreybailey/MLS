#!/usr/bin/env python3
"""
Ingest published school attendance zone polygons into attendance_zones.gpkg.

Reads GeoJSON files downloaded from city ArcGIS portals and merges them
into our SABS-format GeoPackage. School names are matched to NCES IDs
using our existing school_match module.

Usage:
    python scripts/ingest_published_zones.py --source worcester
"""

import argparse
import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from school_match import best_match, match_by_distinctive_token

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), 'data')
ZONES_GPKG = os.path.join(DATA, 'attendance_zones.gpkg')

SOURCES = {
    'worcester': {
        'leaid': '2513230',
        'state': 'MA',
        'levels': {
            'elementary': {
                'url': 'https://services6.arcgis.com/qCN0ld8ZT1YxcVUJ/arcgis/rest/services/WPS_Catchment_Zones__WEBMAP__WFL1/FeatureServer/82/query?where=1%3D1&outFields=*&f=geojson',
                'name_field': 'FullName_Elementary',
            },
            'middle': {
                'url': 'https://services6.arcgis.com/qCN0ld8ZT1YxcVUJ/arcgis/rest/services/WPS_Catchment_Zones__WEBMAP__WFL1/FeatureServer/83/query?where=1%3D1&outFields=*&f=geojson',
                'name_field': 'FullName_Middle',
            },
            'high': {
                'url': 'https://services6.arcgis.com/qCN0ld8ZT1YxcVUJ/arcgis/rest/services/WPS_Catchment_Zones__WEBMAP__WFL1/FeatureServer/84/query?where=1%3D1&outFields=*&f=geojson',
                'name_field': 'FullName_High',
            },
        },
    },
}

LEVEL_CODE = {'elementary': '1', 'middle': '2', 'high': '3'}

# Schools that span multiple levels or have non-standard NCES level classification
MANUAL_OVERRIDES = {
    'worcester': {
        'Claremont Academy': '251323002121',
    },
}


def _download_or_load(url, cache_path):
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)
    import urllib.request
    print(f'  Downloading {url[:80]}...')
    data = urllib.request.urlopen(url).read()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'wb') as f:
        f.write(data)
    return json.loads(data)


def _match_name(name, candidates):
    """Match a zone name to an NCES school using our existing matchers.

    candidates: list of (ncessch, name) tuples.
    Returns (ncessch, matched_name) or None."""
    cand_dicts = [{'ncessch': nc, 'name': n} for nc, n in candidates]

    m = best_match(name, cand_dicts)
    if m:
        return m['ncessch'], m['name']

    m = match_by_distinctive_token(name, cand_dicts)
    if m:
        return m['ncessch'], m['name']

    return None


def ingest_source(source_name, conn, dry_run=False):
    src = SOURCES[source_name]
    leaid = src['leaid']
    state = src['state']

    total_matched = 0
    total_zones = 0
    all_features = []

    for level, cfg in src['levels'].items():
        cache_path = os.path.join(HERE, 'data',
                                  f'{source_name}_{level}_zones.geojson')
        geojson = _download_or_load(cfg['url'], cache_path)
        features = geojson.get('features', [])
        total_zones += len(features)

        nces_schools = conn.execute(
            'SELECT ncessch, name FROM schools WHERE leaid = ? AND level = ?',
            (leaid, level)).fetchall()
        candidates = [(r[0], r[1]) for r in nces_schools]

        grade_info = {}
        for nc, name in nces_schools:
            row = conn.execute(
                'SELECT grade_lo, grade_hi FROM schools WHERE ncessch = ?',
                (nc,)).fetchone()
            if row:
                grade_info[nc] = (row[0] or '?', row[1] or '?')

        print(f'\n  {level}: {len(features)} zones, '
              f'{len(candidates)} NCES candidates')

        for feat in features:
            props = feat.get('properties', {})
            zone_name = props.get(cfg['name_field'], '') or ''
            if not zone_name:
                print(f'    SKIP: no name in feature')
                continue

            overrides = MANUAL_OVERRIDES.get(source_name, {})
            if zone_name in overrides:
                nc = overrides[zone_name]
                row = conn.execute(
                    'SELECT name FROM schools WHERE ncessch = ?', (nc,)
                ).fetchone()
                result = (nc, row[0]) if row else None
            else:
                result = _match_name(zone_name, candidates)
            if result:
                ncessch, matched_name = result
                grades = grade_info.get(ncessch, ('?', '?'))
                feat['properties'] = {
                    'ncessch': ncessch,
                    'schnam': matched_name,
                    'leaid': leaid,
                    'level': LEVEL_CODE[level],
                    'gslo': str(grades[0]),
                    'gshi': str(grades[1]),
                    'openEnroll': 0,
                    'defacto': 0,
                    'source': f'published-{source_name}',
                }
                all_features.append(feat)
                total_matched += 1
                print(f'    OK: {zone_name} -> {matched_name} ({ncessch})')
            else:
                print(f'    MISS: {zone_name}')

    print(f'\n  Matched {total_matched}/{total_zones} zones')

    if dry_run:
        print('  DRY RUN: not writing to GeoPackage')
        return total_matched

    import geopandas as gpd
    from shapely.geometry import shape

    rows = []
    for feat in all_features:
        geom = shape(feat['geometry'])
        p = feat['properties']
        rows.append({**p, 'geometry': geom})

    if not rows:
        print('  No zones to write')
        return 0

    gdf = gpd.GeoDataFrame(rows, crs='EPSG:4326')

    existing = gpd.read_file(ZONES_GPKG, layer=state)
    existing_ncs = set(existing['ncessch'].tolist())
    new_ncs = set(gdf['ncessch'].tolist())
    overlap = existing_ncs & new_ncs
    if overlap:
        print(f'  Replacing {len(overlap)} existing SABS zones with published data')
        existing = existing[~existing['ncessch'].isin(new_ncs)]

    merged = gpd.GeoDataFrame(
        data=list(existing.drop(columns='geometry').to_dict('records'))
             + list(gdf.drop(columns='geometry').to_dict('records')),
        geometry=list(existing.geometry) + list(gdf.geometry),
        crs='EPSG:4326',
    )

    cols = ['ncessch', 'schnam', 'leaid', 'gslo', 'gshi',
            'level', 'openEnroll', 'defacto', 'geometry']
    for c in cols:
        if c not in merged.columns and c != 'geometry':
            merged[c] = None
    merged = merged[cols]

    merged.to_file(ZONES_GPKG, layer=state, driver='GPKG')
    print(f'  Wrote {len(merged)} total zones to {ZONES_GPKG} layer {state}')
    return total_matched


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', required=True, choices=list(SOURCES.keys()))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(db.DB_PATH)
    print(f'Ingesting {args.source} published school zones...')
    matched = ingest_source(args.source, conn, dry_run=args.dry_run)
    conn.close()

    if matched == 0:
        print('ERROR: no zones matched')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
