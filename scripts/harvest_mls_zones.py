#!/usr/bin/env python3
"""
Harvest school assignment labels from Realtor.com MLS listing metadata.

For each MA city, scrapes for_sale listings and extracts the "School
Information" detail field. Each listing with a specific elementary school
name becomes a labeled GPS point: (lat, lon) -> school_name.

The output CSV is the expensive artifact — the scrape takes ~6-10 hours.
Commit it so downstream steps are reproducible without re-scraping.

Resumable: skips cities already in the output CSV.

Usage:
    python scripts/harvest_mls_zones.py
    python scripts/harvest_mls_zones.py --state MA --past-days 180
    python scripts/harvest_mls_zones.py --limit-cities 10  # test run
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homeharvest import scrape_property

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CITIES_CSV = os.path.join(ROOT, 'us_cities.csv')
OUTPUT_CSV = os.path.join(HERE, 'data', 'mls_school_labels.csv')

VAGUE_LABELS = {
    '', 'n/a', 'none', 'other', 'unknown',
    'ranked choice', 'school choice', 'choice system', 'lottery',
    'choice', 'open enrollment', 'assigned',
    'per board of ed', 'per boe', 'pboe', 'school board',
    'local', 'apply', 'public/private', 'public & private',
    'see dept of ed', 'see supt', 'see remarks', 'tbd',
    'buyer to verify', 'contact listing agent', 'check with listing agent',
    'none selected', 'flex zone', 'priority enrollment',
}

FIELDNAMES = ['lat', 'lon', 'city', 'state', 'address',
              'elementary', 'middle', 'high', 'property_url']


def _extract_school_info(listing):
    """Extract school names from a raw Realtor.com listing dict."""
    details = listing.get('details', [])
    if not details:
        return {}
    for d in details:
        if d.get('category') == 'School Information':
            schools = {}
            for text in d.get('text', []):
                parts = text.split(':', 1)
                if len(parts) != 2:
                    continue
                key = parts[0].strip().lower()
                val = parts[1].strip()
                if 'elementary' in key:
                    schools['elementary'] = val
                elif 'middle' in key or 'junior' in key:
                    schools['middle'] = val
                elif 'high' in key:
                    schools['high'] = val
            return schools
    return {}


def _is_specific(name):
    """Is this a specific school name (not a vague/choice label)?"""
    if not name:
        return False
    normalized = name.strip().lower()
    if normalized in VAGUE_LABELS:
        return False
    if len(normalized) <= 3:
        return False
    return True


def _get_coords(listing):
    """Extract lat/lon from a raw listing dict."""
    loc = listing.get('location', {})
    addr = loc.get('address', {})
    coord = addr.get('coordinate', {})
    if coord:
        return coord.get('lat'), coord.get('lon')
    # Fallback: top-level fields (varies by homeharvest version)
    return listing.get('latitude'), listing.get('longitude')


def _get_address(listing):
    loc = listing.get('location', {})
    addr = loc.get('address', {})
    return addr.get('line', '')


def _get_url(listing):
    return listing.get('href', '') or str(listing.get('property_url', ''))


def load_completed_cities(path):
    """Load cities already scraped from the output CSV."""
    if not os.path.exists(path):
        return set()
    cities = set()
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            cities.add(f"{row['city']}, {row['state']}")
    return cities


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--past-days', type=int, default=180)
    ap.add_argument('--limit-cities', type=int, default=0,
                    help='stop after N cities (0 = all)')
    ap.add_argument('--delay', type=float, default=1.5,
                    help='seconds between city scrapes')
    args = ap.parse_args()

    # Load city list
    cities = []
    with open(CITIES_CSV, newline='') as f:
        for row in csv.DictReader(f):
            if row['state'] == args.state:
                cities.append(row)

    print(f"Found {len(cities)} cities in {args.state}")

    # Resume support
    completed_cities = load_completed_cities(OUTPUT_CSV)
    # Track completed by "City, ST" key
    completed_keys = set()
    for city in cities:
        key = f"{city['city']}, {args.state}"
        if key in completed_cities:
            completed_keys.add(key)

    remaining = [c for c in cities
                 if f"{c['city']}, {args.state}" not in completed_keys]
    if completed_keys:
        print(f"Resuming: {len(completed_keys)} cities already done, "
              f"{len(remaining)} remaining")

    if args.limit_cities:
        remaining = remaining[:args.limit_cities]

    # Open output file (append mode for resume)
    write_header = not os.path.exists(OUTPUT_CSV) or os.path.getsize(OUTPUT_CSV) == 0
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    outfile = open(OUTPUT_CSV, 'a', newline='')
    writer = csv.DictWriter(outfile, fieldnames=FIELDNAMES)
    if write_header:
        writer.writeheader()

    total_labels = 0
    total_listings = 0
    errors = 0

    for i, city_row in enumerate(remaining):
        city_name = city_row['city']
        location = f"{city_name}, {args.state}"

        try:
            results = scrape_property(
                location, listing_type='for_sale',
                past_days=args.past_days,
                extra_property_data=True,
                limit=200,
                return_type='raw')
        except Exception as e:
            print(f"  [{i+1}/{len(remaining)}] {location}: ERROR {e}")
            errors += 1
            time.sleep(args.delay)
            continue

        city_labels = 0
        for listing in results:
            total_listings += 1
            schools = _extract_school_info(listing)
            if not schools:
                continue

            elem = schools.get('elementary', '')
            mid = schools.get('middle', '')
            high = schools.get('high', '')

            if not any(_is_specific(s) for s in (elem, mid, high)):
                continue

            lat, lon = _get_coords(listing)
            if lat is None or lon is None:
                continue

            writer.writerow({
                'lat': round(lat, 6),
                'lon': round(lon, 6),
                'city': city_name,
                'state': args.state,
                'address': _get_address(listing),
                'elementary': elem if _is_specific(elem) else '',
                'middle': mid if _is_specific(mid) else '',
                'high': high if _is_specific(high) else '',
                'property_url': _get_url(listing),
            })
            city_labels += 1
            total_labels += 1

        outfile.flush()
        status = f"{city_labels} labels from {len(results)} listings"
        print(f"  [{i+1}/{len(remaining)}] {location}: {status}")
        time.sleep(args.delay)

    outfile.close()

    print(f"\nDone: {total_labels} labeled points from {total_listings} "
          f"listings across {len(remaining)} cities")
    if errors:
        print(f"  {errors} cities failed (re-run to retry)")
    print(f"Output: {OUTPUT_CSV}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
