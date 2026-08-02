#!/usr/bin/env python3
"""
Fill rating gaps with MCAS (Massachusetts state test) proficiency data.

GreatSchools doesn't rate every school.  MCAS results are published by the
MA Department of Elementary and Secondary Education for every school that
administers the state test, so they cover most of the gap.  This script:

  1. Downloads the NCES CCD directory to get the ncessch -> DESE org_code
     crosswalk (the `seasch` field).
  2. Downloads school-level MCAS "Meeting or Exceeding Expectations"
     percentages from the DESE Socrata portal (E2C Hub).
  3. Converts the weighted-average proficiency to a 1-10 rating calibrated
     against the observed GS-rating-to-MCAS-proficiency distribution.
  4. Stores only for schools that have NO existing rating (source priority
     in db.put_school_rating ensures MCAS never overwrites GreatSchools or
     manual entries).

The conversion is calibrated from 1,566 MA schools that have both a
GreatSchools rating and 2025 MCAS results.  Median proficiency per GS tier:

    GS  1   2   3   4   5   6   7   8   9  10
       6% 15% 19% 25% 34% 42% 51% 60% 69% 77%

Thresholds are midpoints between adjacent medians.

Usage:
    python scripts/backfill_mcas.py --state MA
    python scripts/backfill_mcas.py --state MA --mcas-year 2025
    python scripts/backfill_mcas.py --state MA --dry-run
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

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

# Midpoints between adjacent GS-tier median proficiencies.
_THRESHOLDS = [
    (0.105, 2), (0.17, 3), (0.22, 4), (0.295, 5),
    (0.38, 6), (0.465, 7), (0.555, 8), (0.645, 9), (0.73, 10),
]


def proficiency_to_rating(pct: float) -> int:
    for threshold, rating in _THRESHOLDS:
        if pct < threshold:
            return rating - 1
    return 10


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'school-rental-search/1.0'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def build_crosswalk(state: str, ccd_year: int) -> dict:
    """ncessch -> DESE 8-digit org_code, via the CCD `seasch` field."""
    fips = STATE_FIPS[state.upper()]
    url = (f'https://educationdata.urban.org/api/v1/schools/ccd/directory/'
           f'{ccd_year}/?fips={fips}&per_page=5000')
    mapping = {}
    while url:
        data = _fetch_json(url)
        for r in data.get('results', []):
            seasch = r.get('seasch', '')
            ncessch = r.get('ncessch', '')
            if '-' in seasch:
                dese = seasch.split('-')[-1]
            else:
                dese = seasch
            if ncessch and dese:
                mapping[ncessch] = dese
        url = data.get('next')
        time.sleep(0.3)
    return mapping


def fetch_mcas(year: int) -> dict:
    """DESE org_code -> weighted-average M+E proficiency across ELA + Math."""
    base = ('https://educationtocareer.data.mass.gov/resource/i9w6-niyt.csv'
            f'?$where=org_type%20in(%27Public%20School%27,%27Charter%20School%27)'
            f'%20AND%20stu_grp=%27All%20Students%27%20AND%20sy=%27{year}%27'
            f'%20AND%20subject_code%20in(%27ELA%27,%27MATH%27)'
            f'&$select=org_code,org_name,m_plus_e_pct,stu_cnt'
            f'&$limit=50000')
    req = urllib.request.Request(base, headers={'User-Agent': 'school-rental-search/1.0'})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode('utf-8')
    schools = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = row['org_code']
        pct, cnt = row['m_plus_e_pct'], row['stu_cnt']
        if not pct or not cnt:
            continue
        pct, cnt = float(pct), int(cnt)
        if code not in schools:
            schools[code] = {'name': row['org_name'], 'w': 0, 'n': 0}
        schools[code]['w'] += pct * cnt
        schools[code]['n'] += cnt
    for s in schools.values():
        s['proficiency'] = s['w'] / s['n'] if s['n'] else 0
    return schools


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--ccd-year', type=int, default=2022,
                    help='CCD directory year for crosswalk (default 2022)')
    ap.add_argument('--mcas-year', type=int, default=2025,
                    help='MCAS results year (default 2025)')
    ap.add_argument('--primary', action='store_true',
                    help='rate ALL schools with MCAS data, not just unrated')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    state = args.state.upper()

    print(f"Building NCES -> DESE crosswalk ({args.ccd_year})...")
    crosswalk = build_crosswalk(state, args.ccd_year)
    print(f"  {len(crosswalk)} schools mapped")

    print(f"Fetching MCAS {args.mcas_year} results...")
    mcas = fetch_mcas(args.mcas_year)
    print(f"  {len(mcas)} schools with data")

    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    if args.primary:
        candidates = conn.execute(
            'SELECT s.ncessch, s.name FROM schools s WHERE s.state = ?',
            (state,)).fetchall()
    else:
        candidates = conn.execute(
            'SELECT s.ncessch, s.name FROM schools s '
            'LEFT JOIN school_ratings r ON s.ncessch = r.ncessch '
            'WHERE s.state = ? AND r.ncessch IS NULL', (state,)).fetchall()
    conn.close()

    filled = 0
    for ncessch, name in candidates:
        dese = crosswalk.get(ncessch, '')
        if dese not in mcas:
            continue
        m = mcas[dese]
        rating = proficiency_to_rating(m['proficiency'])
        if args.dry_run:
            print(f"  would rate {name:<45} prof={m['proficiency']:.0%} -> {rating}")
        else:
            db.put_school_rating(ncessch, rating,
                                 f"MCAS {args.mcas_year} M+E={m['proficiency']:.0%}",
                                 source='mcas')
            print(f"  {name:<45} prof={m['proficiency']:.0%} -> {rating}")
        filled += 1

    mode = "primary" if args.primary else "gap-fill"
    verb = "would rate" if args.dry_run else "rated"
    print(f"\n{verb} {filled}/{len(candidates)} schools from MCAS data ({mode} mode)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
