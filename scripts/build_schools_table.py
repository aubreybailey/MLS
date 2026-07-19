#!/usr/bin/env python3
"""
Populate the `schools` directory table from NCES CCD.

Why this table exists: attendance zones (SABS) identify the assigned school by
`ncessch`, but we had nowhere to look that id up. Without it we could say "this
address is zoned for Thompson" and still fail to attach a rating, because the
GreatSchools radius query happened not to include Thompson. This table is the
join target -- authoritative name, coordinates, grade span and district for
every public school -- so ratings can be stored per school instead of per
map tile.

Source: NCES Common Core of Data, served by the Urban Institute Education Data
API (free, no key).
    https://educationdata.urban.org/documentation/

Usage:
    python scripts/build_schools_table.py --state MA
    python scripts/build_schools_table.py --state MA --year 2022
"""

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

API = ('https://educationdata.urban.org/api/v1/schools/ccd/directory/'
       '{year}/?fips={fips}&per_page=500')

STATE_FIPS = {
    "AL": 1, "AK": 2, "AZ": 4, "AR": 5, "CA": 6, "CO": 8, "CT": 9, "DE": 10,
    "DC": 11, "FL": 12, "GA": 13, "HI": 15, "ID": 16, "IL": 17, "IN": 18,
    "IA": 19, "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24, "MA": 25,
    "MI": 26, "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31, "NV": 32,
    "NH": 33, "NJ": 34, "NM": 35, "NY": 36, "NC": 37, "ND": 38, "OH": 39,
    "OK": 40, "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46, "TN": 47,
    "TX": 48, "UT": 49, "VT": 50, "VA": 51, "WA": 53, "WV": 54, "WI": 55,
    "WY": 56,
}


def classify(lo, hi) -> str:
    """Grade span -> level, matching how the app talks about schools."""
    if lo is None or hi is None:
        return 'other'
    if hi <= 6 and lo <= 3:
        return 'elementary'
    if lo >= 4 and hi <= 9:
        return 'middle'
    if lo >= 8:
        return 'high'
    return 'other'


def fetch_state(state: str, year: int, verbose: bool = True) -> list:
    fips = STATE_FIPS[state.upper()]
    url = API.format(year=year, fips=fips)
    out = []
    while url:
        req = urllib.request.Request(url, headers={'User-Agent': 'school-rental-search/1.0'})
        with urllib.request.urlopen(req, timeout=90) as r:
            payload = json.load(r)
        out += payload.get('results', [])
        url = payload.get('next')
        if verbose:
            print(f"\r  fetched {len(out)} schools...", end='', flush=True)
        time.sleep(0.3)
    if verbose:
        print()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--year', type=int, default=2022,
                    help='CCD collection year (default 2022)')
    args = ap.parse_args()

    state = args.state.upper()
    if state not in STATE_FIPS:
        print(f"Unknown state: {state}")
        return 1

    print(f"Fetching NCES school directory for {state} ({args.year})...")
    raw = fetch_state(state, args.year)
    if not raw:
        print("No schools returned.")
        return 1

    rows, no_coords = [], 0
    for s in raw:
        lat, lon = s.get('latitude'), s.get('longitude')
        if lat is None or lon is None:
            no_coords += 1
        lo, hi = s.get('lowest_grade_offered'), s.get('highest_grade_offered')
        rows.append({
            'ncessch': s.get('ncessch'),
            'name': s.get('school_name') or '',
            'leaid': s.get('leaid'),
            'state': state,
            'city': s.get('city_location') or '',
            'lat': lat, 'lon': lon,
            'grade_lo': lo, 'grade_hi': hi,
            'level': classify(lo, hi),
            'enrollment': s.get('enrollment'),
            'source': 'nces',
        })

    written = db.upsert_schools([r for r in rows if r['ncessch']])
    print(f"\nWrote {written} schools to {db.DB_PATH}")
    if no_coords:
        print(f"  ({no_coords} had no coordinates and won't appear in proximity queries)")

    by_level = {}
    for r in rows:
        by_level[r['level']] = by_level.get(r['level'], 0) + 1
    for lvl in ('elementary', 'middle', 'high', 'other'):
        if lvl in by_level:
            print(f"  {lvl:<11} {by_level[lvl]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
