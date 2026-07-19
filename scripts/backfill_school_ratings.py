#!/usr/bin/env python3
"""
Fetch GreatSchools ratings for the schools nearest a location and store them
per school in the local database.

Ratings previously existed only as a ~3 mile *area average* attached to a map
tile. Once attendance zones tell us which school an address is actually zoned
for, we need that specific school's rating -- so this walks the schools table
outward from a point, queries GreatSchools, matches each result back to an NCES
school id, and records it.

Matching is conservative (scripts/school_match.py): ambiguous names are skipped
rather than guessed, because attaching the wrong school's rating is worse than
having none.

Usage:
    python scripts/backfill_school_ratings.py --location "Northborough, MA" --limit 100
    python scripts/backfill_school_ratings.py --lat 42.3195 --lon -71.6459 --level elementary
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from greatschools_scraper import get_ratings_by_level
from school_match import best_match


def geocode(location: str):
    """Resolve a place name via the app's existing geocoder."""
    from search import geocode_location
    lat, lon, _state = geocode_location(location)
    return lat, lon


def cells_for(schools: list, precision: int = 2) -> list:
    """Group schools into ~1km cells so we issue one GreatSchools query per
    neighbourhood instead of one per school."""
    seen, out = set(), []
    for s in schools:
        key = (round(s['lat'], precision), round(s['lon'], precision))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def haversine(a, b, c, d):
    import math
    R = 3958.8
    x = (math.sin(math.radians(c - a) / 2) ** 2
         + math.cos(math.radians(a)) * math.cos(math.radians(c))
         * math.sin(math.radians(d - b) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(x))


def backfill_state(state: str, level: str, delay: float, max_queries: int,
                   query_radius: float = 2.5) -> int:
    """Cover every school in a state with as few queries as possible.

    Each GreatSchools call already returns everything within ~3 miles, so
    querying per school (or per 1km cell) wastes an order of magnitude of
    requests. Instead: walk the unrated schools, query at one, let that call
    rate all its neighbours, and skip anything already covered by a previous
    query point. That turns ~1900 schools into a few hundred requests.
    """
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    sql = ('SELECT ncessch, name, lat, lon FROM schools '
           'WHERE state = ? AND lat IS NOT NULL')
    args = [state]
    if level:
        sql += ' AND level = ?'
        args.append(level)
    targets = [dict(zip(('ncessch', 'name', 'lat', 'lon'), r))
               for r in conn.execute(sql, args)]
    conn.close()

    print(f"{len(targets)} {level or 'total'} schools in {state}.")
    queried = []          # points we've already covered
    rated = unmatched = queries = 0

    for i, s in enumerate(targets):
        if queries >= max_queries:
            print(f"\nHit --max-queries ({max_queries}); re-run to continue.")
            break
        if db.get_school_rating(s['ncessch']) is not None:
            continue
        # Already inside a previous query's footprint: if GreatSchools had a
        # rating for this school, that call would have supplied it.
        if any(haversine(s['lat'], s['lon'], q[0], q[1]) < query_radius for q in queried):
            continue

        try:
            payload = get_ratings_by_level(s['lat'], s['lon'])
        except Exception as e:
            print(f"  query failed at {s['name']}: {e}")
            continue
        queries += 1
        queried.append((s['lat'], s['lon']))

        nearby = db.schools_near(s['lat'], s['lon'], 4.0, None, 250)
        got = 0
        for lvl in ('elementary', 'middle', 'high', 'other'):
            for gs in (payload.get(lvl) or {}).get('schools', []) or []:
                if gs.get('rating') is None:
                    continue
                hit = best_match(gs.get('name', ''), nearby)
                if not hit:
                    unmatched += 1
                    continue
                if db.get_school_rating(hit['ncessch']) is None:
                    db.put_school_rating(hit['ncessch'], gs['rating'], gs.get('name', ''))
                    rated += 1
                    got += 1
        print(f"  [{queries}] {s['name'][:34]:<34} +{got:<3} (total {rated}, {i}/{len(targets)} scanned)",
              flush=True)
        time.sleep(delay)

    print(f"\nqueries: {queries}   newly rated: {rated}   unmatched names: {unmatched}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', help='backfill a whole state, e.g. MA')
    ap.add_argument('--delay', type=float, default=0.6,
                    help='seconds between GreatSchools queries (default 0.6)')
    ap.add_argument('--max-queries', type=int, default=2000,
                    help='stop after this many requests; re-run to resume')
    ap.add_argument('--location', help='e.g. "Northborough, MA"')
    ap.add_argument('--lat', type=float)
    ap.add_argument('--lon', type=float)
    ap.add_argument('--radius', type=float, default=10.0, help='miles (default 10)')
    ap.add_argument('--limit', type=int, default=100, help='schools to cover (default 100)')
    ap.add_argument('--level', help='elementary | middle | high')
    ap.add_argument('--refresh', action='store_true', help='re-fetch schools already rated')
    args = ap.parse_args()

    if args.state:
        return backfill_state(args.state.upper(), args.level, args.delay,
                              args.max_queries)

    if args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    elif args.location:
        lat, lon = geocode(args.location)
        if lat is None:
            print(f"Could not geocode {args.location!r}")
            return 1
    else:
        print("Need --location or --lat/--lon")
        return 1

    targets = db.schools_near(lat, lon, args.radius, args.level, args.limit)
    if not targets:
        print("No schools found nearby. Has scripts/build_schools_table.py been run?")
        return 1
    targets = [s for s in targets if s.get('lat') and s.get('lon')]

    if not args.refresh:
        todo = [s for s in targets if db.get_school_rating(s['ncessch']) is None]
    else:
        todo = targets
    print(f"{len(targets)} schools within {args.radius}mi; {len(todo)} need ratings.")
    if not todo:
        return 0

    cells = cells_for(todo)
    print(f"Querying GreatSchools for {len(cells)} ~1km cells...\n")

    rated = skipped = unmatched = 0
    for i, (clat, clon) in enumerate(cells, 1):
        try:
            payload = get_ratings_by_level(clat, clon)
        except Exception as e:
            print(f"  [{i}/{len(cells)}] {clat},{clon} FAILED: {e}")
            continue

        # Candidates: schools we know are near this cell, so a name match is
        # being made against a geographically plausible shortlist.
        nearby = db.schools_near(clat, clon, 4.0, None, 200)
        for level in ('elementary', 'middle', 'high', 'other'):
            for gs in (payload.get(level) or {}).get('schools', []) or []:
                if gs.get('rating') is None:
                    continue
                hit = best_match(gs.get('name', ''), nearby)
                if not hit:
                    unmatched += 1
                    continue
                if not args.refresh and db.get_school_rating(hit['ncessch']) is not None:
                    skipped += 1
                    continue
                db.put_school_rating(hit['ncessch'], gs['rating'], gs.get('name', ''))
                rated += 1
        print(f"  [{i}/{len(cells)}] {clat},{clon} -> {rated} rated so far")
        time.sleep(0.5)

    print(f"\nrated: {rated}   already had: {skipped}   unmatched names: {unmatched}")
    still = [s for s in targets if db.get_school_rating(s['ncessch']) is None]
    print(f"{len(targets) - len(still)}/{len(targets)} target schools now have a rating.")
    if still:
        print("\nStill unrated (GreatSchools has no rating, or the name was ambiguous):")
        for s in still[:15]:
            print(f"  {s['name']:<38} {s['level']:<11} {s['distance_mi']:.1f}mi")
    return 0


if __name__ == '__main__':
    sys.exit(main())
