#!/usr/bin/env python3
"""
Convert the Census TIGER school-district shapefiles into a single GeoPackage.

Why: lookup_coords() used to glob every state's shapefile and loop state by
state doing point-in-polygon until something matched -- up to 50 states scanned
for one coordinate, across 358 files. A GeoPackage is one file with an R-tree
spatial index, so a lookup becomes a single indexed bbox query against a
national layer, with no notion of "which state" at all.

GeoPackage is just SQLite, and geopandas reads/writes it natively, so this adds
no dependencies.

Usage:
    python scripts/build_geopackage.py [--data-dir DIR] [--force]

Output: data/school_districts.gpkg, layers 'unsd' / 'elsd' / 'scsd'.
The source shapefiles are left in place (delete them to reclaim ~237MB).
"""

import argparse
import glob
import os
import sys

import geopandas as gpd
from shapely.geometry import MultiPolygon

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
GPKG_NAME = 'school_districts.gpkg'

# Only the columns the lookup actually reads -- TIGER ships ~18, most unused.
KEEP = ['NAME', 'GEOID', 'LOGRADE', 'HIGRADE', 'geometry']

DISTRICT_TYPES = ['unsd', 'elsd', 'scsd']


def build(data_dir: str, force: bool = False) -> int:
    gpkg_path = os.path.join(data_dir, GPKG_NAME)

    if os.path.exists(gpkg_path) and not force:
        print(f"{gpkg_path} already exists. Use --force to rebuild.")
        return 0

    if os.path.exists(gpkg_path):
        os.remove(gpkg_path)

    total = 0
    for dtype in DISTRICT_TYPES:
        files = sorted(glob.glob(os.path.join(data_dir, f'tl_2023_*_{dtype}.shp')))
        if not files:
            print(f"  {dtype}: no shapefiles found, skipping")
            continue

        print(f"--- {dtype} ({len(files)} states) ---")
        written = 0
        for i, path in enumerate(files):
            fips = os.path.basename(path).split('_')[2]
            try:
                gdf = gpd.read_file(path)
            except Exception as e:
                print(f"  {fips}: read failed ({e}), skipping")
                continue

            if gdf.empty:
                continue

            if gdf.crs and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)

            # Normalize columns: not every TIGER vintage/type has all of them.
            for col in KEEP:
                if col not in gdf.columns:
                    gdf[col] = ''
            gdf = gdf[KEEP]
            gdf['STATE_FIPS'] = fips

            # TIGER mixes Polygon and MultiPolygon within a layer; GeoPackage
            # wants one geometry type per layer. Promote so the file is spec
            # conformant (the driver would otherwise accept it with a warning).
            gdf['geometry'] = gdf.geometry.apply(
                lambda g: MultiPolygon([g]) if g is not None and g.geom_type == 'Polygon' else g
            )

            # Append state by state so peak memory stays flat instead of
            # holding all 237MB of geometry at once.
            gdf.to_file(
                gpkg_path,
                layer=dtype,
                driver='GPKG',
                mode='a' if written else 'w',
            )
            written += len(gdf)
            print(f"\r  {i + 1}/{len(files)} states, {written} districts", end='', flush=True)

        print(f"\r  {len(files)} states, {written} districts{' ' * 20}")
        total += written

    if total == 0:
        print("\nNo districts written -- is the boundary data downloaded? "
              "Run ./download_data.sh first.")
        return 1

    size_mb = os.path.getsize(gpkg_path) / (1024 * 1024)
    print(f"\nWrote {gpkg_path} ({size_mb:.0f}MB, {total} districts).")
    print("The lookup will use it automatically. The tl_2023_*.shp files are no "
          "longer needed and can be deleted.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', default=DATA_DIR, help=f'default: {DATA_DIR}')
    ap.add_argument('--force', action='store_true', help='rebuild if it exists')
    args = ap.parse_args()

    if not os.path.isdir(args.data_dir):
        print(f"Data directory not found: {args.data_dir}")
        return 1
    return build(args.data_dir, args.force)


if __name__ == '__main__':
    sys.exit(main())
