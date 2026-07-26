#!/usr/bin/env python3
"""
Load attendance-zone sample points into the zone_samples table.

These points come from GreatSchools' "Schools by Address" oracle -- its address
search returns the school an address is *assigned* to (marked "Assigned school
in <district>"), which carries current licensed zone data for districts SABS
never covered (Northborough among them). Each point was queried once at a lat/lon
and its assigned elementary recorded; a listing then resolves to the nearest
sample (db.nearest_zone_sample), a labeled-point-cloud classifier that refines as
points are added.

The sample values live in scripts/data/zone_samples.csv (committed) so the
hand-gathered work survives a cache rebuild. School names are re-matched to NCES
on load, since the ncessch depends on the current schools table.

Gathering more points: query
  https://www.greatschools.org/search/search.page?lat=LAT&lon=LON&locationType=street_address&locationLabel=CITY&gradeLevels=e&sort=distance
in a real browser (the JS computes the assignment; curl gets no assignment),
read the school marked "Assigned school in ...", and append a row here.

Usage:
    python scripts/load_zone_samples.py
"""

import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from school_match import best_match, match_by_distinctive_token

CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'zone_samples.csv')


def _match_ncessch(school_name, lat, lon):
    """Resolve a GreatSchools school name to an NCES id, scoping candidates to
    elementary schools near the sample point. The point sits in the assigned
    school's district, so this is a small, correct candidate set -- matching
    'Lincoln Street' against all of MA would hit unrelated Lincoln schools and
    (rightly) refuse."""
    # 8mi (not 4): a midpoint-between-schools sample can sit that far from the
    # assigned school in a large regional district, and the geographic scope is
    # still tight enough that the name match stays unambiguous.
    cands = db.schools_near(lat, lon, 8.0, 'elementary', 100)
    hit = (best_match(school_name, cands) or
           match_by_distinctive_token(school_name, cands))
    return hit['ncessch'] if hit else None


def main():
    if not os.path.exists(CSV):
        print(f"No sample file at {CSV}")
        return 1
    loaded = unmatched = 0
    with open(CSV, newline='') as f:
        for row in csv.DictReader(f):
            lat, lon = float(row['lat']), float(row['lon'])
            nc = _match_ncessch(row['school_name'], lat, lon)
            if nc is None:
                unmatched += 1
            db.put_zone_sample(lat, lon, nc, row['school_name'],
                               row.get('district', ''), row['state'],
                               row.get('source', 'greatschools-assigned'))
            loaded += 1
    print(f"loaded {loaded} zone samples ({unmatched} could not match an NCES school)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
