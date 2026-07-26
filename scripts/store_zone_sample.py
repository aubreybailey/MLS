#!/usr/bin/env python3
"""
Store one attendance-zone sample from a GreatSchools "Schools by Address" page.

Reads the page text on stdin, deterministically extracts the assigned school
(parse_assigned_school), matches it to an NCES school near the point, and writes
a zone_samples row. Nothing here requires judgment, so it can be driven by a
cheap model or a shell loop: browse the URL, pipe the text here.

Usage:
    <browse-text> | python scripts/store_zone_sample.py --lat 42.303 --lon -71.648 --state MA

Prints one line: the school stored (or why it was skipped), so a caller can log
progress without parsing anything.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from greatschools_scraper import parse_assigned_school
from school_match import best_match, match_by_distinctive_token


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--state', default='MA')
    args = ap.parse_args()

    text = sys.stdin.read()
    school_name, district = parse_assigned_school(text)
    if not school_name:
        print(f"SKIP ({args.lat},{args.lon}): no 'Assigned school' on page "
              f"(outside a zoned district, or page didn't render)")
        return 0

    # Match to NCES among elementary schools near the point (small, correct set).
    cands = db.schools_near(args.lat, args.lon, 4.0, 'elementary', 60)
    hit = best_match(school_name, cands) or match_by_distinctive_token(school_name, cands)
    ncessch = hit['ncessch'] if hit else None

    db.put_zone_sample(args.lat, args.lon, ncessch, school_name,
                       district or '', args.state)
    tag = 'matched NCES' if ncessch else 'stored, NO NCES match'
    print(f"OK ({args.lat},{args.lon}): {school_name} [{district}] -> {tag}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
