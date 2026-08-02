#!/usr/bin/env python3
"""
Load MLS-matched school assignment points into the zone_samples table.

Reads mls_school_matched.csv (output of match_mls_to_nces.py) and stores
each successfully matched point via db.put_zone_sample(source='mls-inferred').

Unlike load_zone_samples.py, no re-matching is needed — the matched CSV
already carries ncessch from the matching step.

Usage:
    python scripts/load_mls_zone_samples.py
    python scripts/load_mls_zone_samples.py --dry-run
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(HERE, 'data', 'mls_school_matched.csv')


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(INPUT_CSV):
        print(f"No matched file at {INPUT_CSV}")
        print("Run match_mls_to_nces.py first.")
        return 1

    with open(INPUT_CSV, newline='') as f:
        rows = list(csv.DictReader(f))

    matched = [r for r in rows if r.get('ncessch')]
    print(f"Read {len(rows)} rows, {len(matched)} with NCES match")

    if args.dry_run:
        print(f"\nDry run — would load {len(matched)} zone samples")
        for r in matched[:10]:
            print(f"  ({r['lat']}, {r['lon']}) -> {r['nces_name']} [{r['ncessch']}]")
        return 0

    loaded = 0
    for r in matched:
        db.put_zone_sample(
            lat=float(r['lat']),
            lon=float(r['lon']),
            ncessch=r['ncessch'],
            school_name=r['nces_name'],
            district=r.get('leaid', ''),
            state=r.get('state', 'MA'),
            source='mls-inferred',
        )
        loaded += 1

    print(f"Loaded {loaded} MLS-inferred zone samples into zone_samples table")
    return 0


if __name__ == '__main__':
    sys.exit(main())
