#!/usr/bin/env python3
"""
Build the school attendance-zone layer from NCES SABS.

Attendance zones are what actually determine which school an address feeds
into. They are NOT derivable from district boundaries, and they are NOT
approximated by "nearest school": measured against real SABS zones over 3048
sampled points in Massachusetts multi-school districts, nearest-school picks
the wrong school 43.6% of the time. So we either have a real zone or we say we
don't know.

Source: NCES School Attendance Boundary Survey (SABS), 2015-2016 collection.
    https://nces.ed.gov/programs/edge/SABS

Two important limitations, both surfaced to the user rather than hidden:

  1. VINTAGE. SABS was an experimental survey and was discontinued after
     2015-16, so zones are ~10 years old. Districts that have since redrawn
     boundaries or opened/closed schools will be wrong, and we cannot detect
     which ones.
  2. COVERAGE. Participation was voluntary. In Massachusetts only 192 of 322
     districts with schools responded (60%). Boston, Lowell, Lawrence, Quincy,
     Shrewsbury and Northborough are among those with no zones at all.

Usage:
    python scripts/build_attendance_zones.py --state MA
    python scripts/build_attendance_zones.py --state MA --sabs-zip /path/SABS_1516.zip

Output: data/attendance_zones.gpkg, one layer per state abbreviation.
"""

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

import geopandas as gpd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
GPKG_NAME = 'attendance_zones.gpkg'
SABS_URL = 'https://nces.ed.gov/programs/edge/data/SABS_1516.zip'

# Columns we keep. ncessch joins to NCES school records; leaid joins to the
# TIGER district GEOID we already resolve.
KEEP = ['ncessch', 'schnam', 'leaid', 'gslo', 'gshi', 'level', 'openEnroll', 'defacto', 'geometry']


def _download(dest: str) -> None:
    print(f"Downloading SABS 2015-2016 (~557MB) from {SABS_URL} ...")
    req = urllib.request.Request(SABS_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)
    print(f"  saved {os.path.getsize(dest) / 1048576:.0f}MB")


def build(state: str, sabs_zip: str | None, data_dir: str, force: bool) -> int:
    gpkg = os.path.join(data_dir, GPKG_NAME)
    state = state.upper()

    if os.path.exists(gpkg) and not force:
        try:
            import pyogrio
            if state in {l[0] for l in pyogrio.list_layers(gpkg)}:
                print(f"Layer {state} already in {gpkg}. Use --force to rebuild.")
                return 0
        except Exception:
            pass

    tmpdir = None
    try:
        if not sabs_zip:
            tmpdir = tempfile.mkdtemp(prefix='sabs_')
            sabs_zip = os.path.join(tmpdir, 'SABS_1516.zip')
            _download(sabs_zip)

        if not zipfile.is_zipfile(sabs_zip):
            print(f"Not a zip archive: {sabs_zip}")
            return 1

        extract_dir = tmpdir or tempfile.mkdtemp(prefix='sabs_x_')
        print("Extracting shapefile ...")
        with zipfile.ZipFile(sabs_zip) as z:
            z.extractall(extract_dir)

        shp = None
        for root, _, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith('.shp'):
                    shp = os.path.join(root, f)
                    break
        if not shp:
            print("No .shp found in archive.")
            return 1

        # Push the state filter down to OGR so we never load all 75k national
        # zones (the shapefile alone is 1.2GB).
        print(f"Reading {state} zones ...")
        gdf = gpd.read_file(shp, where=f"stAbbrev='{state}'")
        if gdf.empty:
            print(f"No zones found for {state}. (Not every state participated.)")
            return 1

        gdf = gdf.to_crs(epsg=4326)
        for c in KEEP:
            if c not in gdf.columns:
                gdf[c] = ''
        gdf = gdf[KEEP]

        os.makedirs(data_dir, exist_ok=True)
        gdf.to_file(gpkg, layer=state, driver='GPKG',
                    mode='a' if os.path.exists(gpkg) else 'w')

        n_dist = gdf['leaid'].nunique()
        print(f"\nWrote {len(gdf)} zones ({gdf['ncessch'].nunique()} schools, "
              f"{n_dist} districts) to {gpkg} layer '{state}'.")
        print("Districts that did not participate have no zones; the lookup "
              "reports those as unknown rather than guessing.")
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA', help='state abbreviation (default MA)')
    ap.add_argument('--sabs-zip', help='path to an already-downloaded SABS_1516.zip')
    ap.add_argument('--data-dir', default=DATA_DIR)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()
    return build(args.state, args.sabs_zip, args.data_dir, args.force)


if __name__ == '__main__':
    sys.exit(main())
