#!/usr/bin/env python3
"""
Regression tests for the api.py data-access facade.

Two sections:
  1. Pure functions -- deterministic, no network, no db writes.
  2. Read-only checks against cache/schools.db (skipped with a notice when the
     db hasn't been built).

The 30-key contract test matters most: every CSV column, web table column and
notify field comes from enrich_listing's output dict, so an accidental key
change silently breaks all three consumers.

Run:  python scripts/test_api.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api


ENRICH_KEYS = [
    'address', 'city', 'state', 'zip', 'price', 'beds', 'baths', 'sqft',
    'lat', 'lon', 'url', 'flags',
    'district', 'district_grades', 'district_hs', 'district_hs_grades',
    'elem', 'elem_school', 'elem_best', 'elem_source', 'elem_confirm',
    'mid', 'mid_school', 'mid_best', 'mid_source', 'mid_confirm',
    'high', 'high_school', 'high_best', 'high_source', 'high_confirm',
    'top_school', 'top_rating', 'school_count',
]


def test_pure(fails):
    # --- _source_note: one case per source value
    cases = [
        (('zoned', 8.0, None, 0), ''),
        (('district-sole', 7.0, 7.0, 0), ''),
        (('district-min', 7.0, 9.0, 0), 'not exact - worst case (7.0-9.0 across district)'),
        (('district-min', 7.0, 9.0, 2), 'not exact - worst case (7.0-9.0 across district), 2 unrated'),
        (('district-min', 6.0, 6.0, 0), 'not exact - worst case (6.0 across district)'),
        (('zoned-unrated', 8.9, None, 0), 'school known, no rating available'),
        (('area-avg', 7.4, None, 0), '*confirm elementary - area average only'),
    ]
    for args, want in cases:
        got = api._source_note(*args)
        if got != want:
            fails.append(f"_source_note{args} = {got!r}, expected {want!r}")

    # --- _resolve_level precedence, with schools_in_district stubbed
    real = api.db.schools_in_district
    try:
        zones_rated = {'1': {'school': 'Zoned Elem', 'ncessch': 'X1'}}
        real_rating = api.db.get_school_rating
        api.db.get_school_rating = lambda n, **k: 9.5 if n == 'X1' else None

        # zoned wins outright
        api.db.schools_in_district = lambda l, lv=None: [
            {'name': 'A', 'rating': 2.0}, {'name': 'B', 'rating': 4.0}]
        r = api._resolve_level('elementary', zones_rated, 'LEA1', {}, 5.0)
        if r != (9.5, None, 'Zoned Elem', 'zoned', 0):
            fails.append(f"zoned precedence: {r}")

        # zoned school unrated -> falls to district floor, keeps school name
        api.db.get_school_rating = lambda n, **k: None
        r = api._resolve_level('elementary', zones_rated, 'LEA1', {}, 5.0)
        if r != (2.0, 4.0, 'Zoned Elem', 'district-min', 0):
            fails.append(f"zoned-unrated -> district-min: {r}")

        # sole school in district: exact
        api.db.schools_in_district = lambda l, lv=None: [{'name': 'Only', 'rating': 6.0}]
        r = api._resolve_level('elementary', {}, 'LEA1', {}, 5.0)
        if r != (6.0, 6.0, 'Only', 'district-sole', 0):
            fails.append(f"district-sole: {r}")

        # multi-school with an unrated one: floor + unrated count
        api.db.schools_in_district = lambda l, lv=None: [
            {'name': 'A', 'rating': 3.0}, {'name': 'B', 'rating': 8.0},
            {'name': 'C', 'rating': None}]
        r = api._resolve_level('elementary', {}, 'LEA1', {}, 5.0)
        if r != (3.0, 8.0, '', 'district-min', 1):
            fails.append(f"district-min w/ unrated: {r}")

        # nothing known -> area average passthrough
        api.db.schools_in_district = lambda l, lv=None: []
        r = api._resolve_level('elementary', {}, 'LEA1', {}, 5.5)
        if r != (5.5, None, '', 'area-avg', 0):
            fails.append(f"area-avg fallthrough: {r}")
    finally:
        api.db.schools_in_district = real
        api.db.get_school_rating = real_rating

    # --- enrich_listing flags path (lat=None -> no lookups fire; deterministic)
    listing = {'unit': '2B', 'text': 'shared room in house', 'sqft': 12000,
               'list_price': 1000, 'days_on_mls': 90, 'style': 'CONDO',
               'latitude': None, 'longitude': None,
               'full_street_line': '1 Test St', 'city': 'Testville',
               'state': 'MA', 'zip_code': '01532', 'beds': 2, 'full_baths': 1,
               'property_url': 'http://x'}
    rec = api.enrich_listing(listing)
    if rec['flags'] != 'UNIT|ROOM|SQFT?|PRICE?|OLD(90d)|MULTI':
        fails.append(f"flags: {rec['flags']!r}")
    if list(rec.keys()) != ENRICH_KEYS:
        missing = set(ENRICH_KEYS) - set(rec)
        extra = set(rec) - set(ENRICH_KEYS)
        fails.append(f"enrich key contract broke: missing={missing} extra={extra}")

    # --- search._passes level mapping (import here: search pulls in folium etc.)
    try:
        from search import _passes
        rec = {'elem': 8.0, 'mid': 4.0, 'high': None, 'sqft': 900, 'flags': ''}
        checks = [
            (_passes(rec, 7.0, False, False, 'elementary'), True),
            (_passes(rec, 7.0, False, False, 'middle'), False),
            (_passes(rec, 1.0, False, False, 'high'), False),   # None rating rejects
            (_passes(rec, None, False, False, 'elementary', 1000), False),
            (_passes(rec, None, False, False, 'elementary', 800), True),
        ]
        for i, (got, want) in enumerate(checks):
            if got != want:
                fails.append(f"_passes case {i}: {got} != {want}")
    except ImportError as e:
        print(f"  (skipping _passes tests: {e})")

    # --- haversine sanity: Boston -> Providence ~ 41 mi
    d = api.haversine_miles(42.3601, -71.0589, 41.8240, -71.4128)
    if not (38 < d < 44):
        fails.append(f"haversine Boston-Providence = {d}")


def test_db_backed(fails):
    if not os.path.exists(api.db.DB_PATH):
        print(f"  (skipping db-backed tests: {api.db.DB_PATH} not built)")
        return
    rows = api.get_schools(42.3195, -71.6459, radius_miles=5)
    if not rows:
        print("  (skipping db-backed tests: schools table empty)")
        return

    dists = [r['distance_mi'] for r in rows]
    if dists != sorted(dists):
        fails.append("get_schools not sorted by distance")
    if any(d > 5 for d in dists):
        fails.append("get_schools returned rows beyond radius")
    if not all('rating' in r and 'rating_source' in r for r in rows):
        fails.append("get_schools rows missing rating join keys")

    elem = api.get_schools(42.3195, -71.6459, 5, level='elementary')
    if any(r['level'] != 'elementary' for r in elem):
        fails.append("level filter leaked non-elementary rows")
    if len(api.get_schools(42.3195, -71.6459, 5, limit=3)) > 3:
        fails.append("limit not applied")

    s = api.get_school(rows[0]['ncessch'])
    if s is None or s['name'] != rows[0]['name'] or s['leaid'] != rows[0]['leaid']:
        fails.append(f"get_school roundtrip mismatch: {s}")

    st = api.get_stats()
    if 'path' not in st or 'namespaces' not in st:
        fails.append(f"get_stats shape: {st}")


def main():
    fails = []
    test_pure(fails)
    test_db_backed(fails)
    if fails:
        print(f"FAILED {len(fails)}")
        for f in fails:
            print(f"  {f}")
        return 1
    print("ok - api tests passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
