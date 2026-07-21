#!/usr/bin/env python3
"""
Build us_cities.csv (the search-box autocomplete list) from Census Gazetteer
files.

Why two sources: the Places gazetteer covers incorporated cities and CDPs for
every state, but in New England the primary municipality is the TOWN, which is
a county subdivision (MCD), not a "place". So Natick, Wayland, Ashland and most
other MA towns are simply absent from Places. The County Subdivisions gazetteer
carries them. We merge Places (national) with County Subdivisions (New England
only, where the town is the real searchable unit) and de-duplicate.

Restricting the county-subdivision supplement to the six New England states is
deliberate: elsewhere (NY/NJ/PA/MI/...) MCDs are townships that overlap the
incorporated places already in the Places file, and adding them would create
confusing duplicates.

Usage:
    python scripts/build_cities.py                 # downloads the gazetteer files
    python scripts/build_cities.py --places-txt X --cousubs-txt Y   # use local copies

Output: us_cities.csv with columns city,state,lat,lon (properly quoted).
"""

import argparse
import csv
import io
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'us_cities.csv')

GAZ = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer"
PLACES_URL = f"{GAZ}/2023_Gaz_place_national.zip"
COUSUBS_URL = f"{GAZ}/2023_Gaz_cousubs_national.zip"

NEW_ENGLAND = {'CT', 'ME', 'MA', 'NH', 'RI', 'VT'}

# Trailing NAME tokens that are place-type labels, not part of the name. Matched
# case-sensitively on purpose: the lowercase forms are Census type words, while
# capitalized 'City'/'Town' are real name parts ('Kansas City', 'Barnstable
# Town', 'Boys Town'). 'CDP' is the one uppercase type label.
TYPE_TOKENS = {'city', 'town', 'village', 'borough', 'township', 'municipality',
               'corporation', 'comunidad', 'urbana', 'plantation', 'CDP'}

# County-subdivision types we keep -- the populated municipalities. Plantations,
# grants, gores, purchases, locations and unorganized territories in northern
# New England are tiny/unpopulated and would only add noise to autocomplete.
COUSUB_KEEP = {'town', 'city'}


def _strip_type(name: str) -> str:
    """Drop one trailing Census type token, preserving proper-noun 'City'/'Town'.

    Single pass: 'Barnstable Town city' -> 'Barnstable Town' (strips lowercase
    'city', keeps 'Town'); 'Kansas City' -> 'Kansas City' (capital 'City' kept)."""
    parts = name.split()
    if len(parts) > 1 and parts[-1] in TYPE_TOKENS:
        return ' '.join(parts[:-1])
    return name


def _read_gaz(text: str):
    """Yield (state, raw_name, lat, lon) from a tab-separated gazetteer file.

    Fields are space-padded and the header/rows carry trailing whitespace, so
    every value is stripped. INTPTLAT/INTPTLONG are the last two columns."""
    reader = csv.reader(io.StringIO(text), delimiter='\t')
    header = [h.strip() for h in next(reader)]
    idx = {name: i for i, name in enumerate(header)}
    si, ni = idx['USPS'], idx['NAME']
    lai, loi = idx['INTPTLAT'], idx['INTPTLONG']
    for row in reader:
        if len(row) <= loi:
            continue
        state = row[si].strip()
        name = row[ni].strip()
        try:
            lat = float(row[lai].strip())
            lon = float(row[loi].strip())
        except ValueError:
            continue
        yield state, name, lat, lon


def _load(path_or_url: str, is_url: bool) -> str:
    if is_url:
        print(f"  downloading {path_or_url}")
        with urllib.request.urlopen(path_or_url, timeout=120) as r:
            data = r.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        inner = next(n for n in zf.namelist() if n.endswith('.txt'))
        return zf.read(inner).decode('latin-1')
    with open(path_or_url, encoding='latin-1') as f:
        return f.read()


def build(places_src, cousubs_src, places_is_url, cousubs_is_url) -> int:
    # (name_lower, state) -> (name, state, lat, lon). Places first so an
    # incorporated place wins over a same-named county subdivision.
    cities = {}

    print("Places (all states):")
    n_places = 0
    for state, raw, lat, lon in _read_gaz(_load(places_src, places_is_url)):
        if 'not defined' in raw.lower():
            continue
        name = _strip_type(raw)
        if not name:
            continue
        cities.setdefault((name.lower(), state), (name, state, lat, lon))
        n_places += 1
    print(f"  {n_places} rows -> {len(cities)} unique")

    print("County subdivisions (New England towns):")
    n_added = 0
    for state, raw, lat, lon in _read_gaz(_load(cousubs_src, cousubs_is_url)):
        if state not in NEW_ENGLAND:
            continue
        parts = raw.split()
        if not parts or parts[-1] not in COUSUB_KEEP:
            continue
        name = _strip_type(raw)
        if not name or 'not defined' in name.lower():
            continue
        key = (name.lower(), state)
        if key not in cities:
            cities[key] = (name, state, lat, lon)
            n_added += 1
    print(f"  +{n_added} New England towns not already present")

    rows = sorted(cities.values(), key=lambda r: (r[1], r[0]))
    with open(OUT, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['city', 'state', 'lat', 'lon'])
        for name, state, lat, lon in rows:
            w.writerow([name, state, f'{lat:.5f}', f'{lon:.5f}'])

    print(f"\nWrote {len(rows)} places to {OUT}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--places-txt', help='local 2023_Gaz_place_national.txt')
    ap.add_argument('--cousubs-txt', help='local 2023_Gaz_cousubs_national.txt')
    args = ap.parse_args()

    places = args.places_txt or PLACES_URL
    cousubs = args.cousubs_txt or COUSUBS_URL
    return build(places, cousubs, args.places_txt is None, args.cousubs_txt is None)


if __name__ == '__main__':
    sys.exit(main())
