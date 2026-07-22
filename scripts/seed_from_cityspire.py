#!/usr/bin/env python3
"""
Seed school_ratings from the CitySpire project's pre-scraped GreatSchools dump.

A bootstrap layer: instant (partial) coverage for states we haven't swept yet,
so a brand-new state isn't blank while its fresh scrape runs. Every fresh
GreatSchools rating and every hand-entered one outranks it (see
db.SOURCE_PRIORITY), so seeding is safe to run at any time -- it only fills
holes, never overwrites current data.

Two limitations, both surfaced rather than hidden:
  * VINTAGE. The dump is ~2020, stored as source='cityspire-2020', and should
    be shown as stale wherever it's the rating in use.
  * COVERAGE. It's ~22k public schools nationally (major cities only), not the
    ~100k universe -- e.g. only 283 MA public schools vs the 1000+ we scrape.

Source: https://github.com/jiobu1/labspt15-cityspire-g-ds
    notebooks/datasets/data/schools/csv/final_school.csv

The CSV has no coordinates, only an address string, so matching is name within
(state, city) plus grade -- restricting candidates to one city keeps the name
match safe without geo.

Usage:
    python scripts/seed_from_cityspire.py                 # all states
    python scripts/seed_from_cityspire.py --state OH      # one state
    python scripts/seed_from_cityspire.py --csv local.csv
"""

import argparse
import csv
import io
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from school_match import best_match, match_by_distinctive_token

CSV_URL = ('https://raw.githubusercontent.com/jiobu1/labspt15-cityspire-g-ds/'
           'main/notebooks/datasets/data/schools/csv/final_school.csv')
SOURCE = 'cityspire-2020'


def _load_csv(path_or_url, is_url):
    if is_url:
        print(f"Downloading CitySpire school dump ({CSV_URL.rsplit('/', 1)[-1]})...")
        with urllib.request.urlopen(path_or_url, timeout=120) as r:
            text = r.read().decode('utf-8', 'replace')
    else:
        with open(path_or_url, encoding='utf-8', errors='replace') as f:
            text = f.read()
    return list(csv.DictReader(io.StringIO(text)))


def _candidates_by_city(state: str):
    """NCES schools grouped by lowercased city, for the target state."""
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    rows = conn.execute(
        'SELECT ncessch, name, city, lat, lon, grade_lo, grade_hi, level '
        'FROM schools WHERE state = ?', (state,)).fetchall()
    conn.close()
    keys = ('ncessch', 'name', 'city', 'lat', 'lon', 'grade_lo', 'grade_hi', 'level')
    by_city = {}
    for r in rows:
        d = dict(zip(keys, r))
        by_city.setdefault((d['city'] or '').strip().lower(), []).append(d)
    return by_city


def seed(rows, only_state=None):
    # Group CSV rows by state so we build each state's candidate index once.
    by_state = {}
    for row in rows:
        st = (row.get('State') or '').strip().upper()
        if not st or (only_state and st != only_state):
            continue
        score = (row.get('Score') or '').strip()
        if 'ublic' not in (row.get('Type') or '') or not score:
            continue
        try:
            rating = float(score)
        except ValueError:
            continue
        # GreatSchools rates 1-10; the CSV codes "no rating" as 0, which would
        # otherwise poison a district worst-case floor. Treat <1 as unrated.
        if rating < 1:
            continue
        by_state.setdefault(st, []).append((row, rating))

    seeded = skipped_have = unmatched = 0
    for st, items in sorted(by_state.items()):
        by_city = _candidates_by_city(st)
        if not by_city:
            continue                          # this state's schools table isn't built
        state_seeded = 0
        for row, rating in items:
            city = (row.get('City') or '').strip().lower()
            cands = by_city.get(city, [])
            if not cands:
                unmatched += 1
                continue
            grades = row.get('Grades') or ''
            hit = (best_match(row.get('School', ''), cands, gs_grades=grades)
                   or match_by_distinctive_token(row.get('School', ''), cands, gs_grades=grades))
            if not hit:
                unmatched += 1
                continue
            if db.get_school_rating(hit['ncessch']) is not None:
                skipped_have += 1            # already have equal/higher priority
                continue
            db.put_school_rating(hit['ncessch'], rating, row.get('School', ''), SOURCE)
            seeded += 1
            state_seeded += 1
        print(f"  {st}: seeded {state_seeded} (of {len(items)} scored public rows)")

    print(f"\nseeded {seeded}   already covered {skipped_have}   "
          f"unmatched (no city/name) {unmatched}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', help='seed only this state (e.g. OH)')
    ap.add_argument('--csv', help='local final_school.csv instead of downloading')
    args = ap.parse_args()
    rows = _load_csv(args.csv or CSV_URL, args.csv is None)
    return seed(rows, args.state.upper() if args.state else None)


if __name__ == '__main__':
    sys.exit(main())
