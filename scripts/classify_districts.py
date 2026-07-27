#!/usr/bin/env python3
"""
Populate districts.zoning_style for a state.

How a district assigns schools decides whether zone sampling is even meaningful.
This records that per district so we stop re-sampling choice districts (where a
sample would pin every address to one arbitrary school) and can label listings
honestly.

Classification, in order:
  single  one rated school at the elementary level -> assignment is trivial
  zoned   covered by SABS, OR we sampled it and found >=2 distinct assigned
          schools (real geographic zones)
  choice  a known regional/choice district, OR sampled and every point returned
          the SAME school (no geographic zoning) -- see KNOWN_CHOICE
  unknown none of the above yet

Usage:
    python scripts/classify_districts.py --state MA
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

# Districts confirmed to assign non-geographically (verified: distant points all
# return one school). Keyed by NCES leaid.
KNOWN_CHOICE = {
    '2502790': 'Boston (home-based assignment lottery)',
    '2501710': 'Acton-Boxborough (regional, non-geographic elementary assignment)',
}


def _sabs_leaids(state: str) -> set:
    """Districts covered by the NCES SABS attendance boundary survey.
    Tries the GeoPackage first (needs geopandas), then falls back to districts
    already tagged with source='sabs' in a prior run (the note persists)."""
    try:
        import geopandas as gpd
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'data', 'attendance_zones.gpkg')
        return set(gpd.read_file(path, layer=state.upper())['leaid'])
    except Exception:
        pass
    # Fallback: districts previously classified from SABS
    try:
        conn = sqlite3.connect(db.DB_PATH)
        return {r[0] for r in conn.execute(
            "SELECT leaid FROM districts WHERE state = ? AND source = 'sabs'",
            (state.upper(),))}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    args = ap.parse_args()
    state = args.state.upper()

    conn = sqlite3.connect(db.DB_PATH)
    sabs = _sabs_leaids(state)

    # elementary school count per district + district name
    rows = conn.execute(
        "SELECT leaid, MIN(name) FROM ("
        "  SELECT s.leaid, s.name FROM schools s"
        "  JOIN school_ratings r ON r.ncessch = s.ncessch"
        "  WHERE s.state = ? AND s.level = 'elementary') GROUP BY leaid", (state,)).fetchall()
    counts = dict(conn.execute(
        "SELECT s.leaid, COUNT(*) FROM schools s JOIN school_ratings r ON r.ncessch = s.ncessch "
        "WHERE s.state = ? AND s.level = 'elementary' GROUP BY s.leaid", (state,)).fetchall())

    # distinct assigned schools we sampled per district
    sampled = {}
    for leaid, nc in conn.execute(
            "SELECT s.leaid, z.ncessch FROM zone_samples z "
            "JOIN schools s ON s.ncessch = z.ncessch WHERE z.ncessch IS NOT NULL"):
        sampled.setdefault(leaid, set()).add(nc)
    conn.close()

    tally = {}
    for leaid, name in rows:
        n = counts.get(leaid, 0)
        if leaid in KNOWN_CHOICE:
            style = 'choice'
            src, note = 'manual-research', KNOWN_CHOICE[leaid]
        elif n <= 1:
            style, src, note = 'single', 'computed-single', None
        elif leaid in sabs:
            style, src, note = 'zoned', 'sabs', 'SABS attendance boundaries'
        elif len(sampled.get(leaid, ())) >= 2:
            style, src, note = 'zoned', 'computed-sampled', 'sampled: multiple assigned schools'
        else:
            style, src, note = 'unknown', 'computed-count', None
        db.set_district_zoning(leaid, style, name=name, state=state, note=note,
                               source=src)
        tally[style] = tally.get(style, 0) + 1

    print(f"{state}: classified {len(rows)} districts")
    for style in ('zoned', 'choice', 'single', 'unknown'):
        if style in tally:
            print(f"  {style:<8} {tally[style]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
