#!/usr/bin/env python3
"""
Data-access facade: every read the app performs goes through this module.

The rest of the codebase divides cleanly around it:
  - api.py (this file): db tables, GPKG district/zone lookups, GreatSchools,
    Nominatim/Overpass, and homeharvest listings -- all reachable as plain
    function calls with scalar/dict arguments and JSON-serializable returns.
  - search.py: quota-fill orchestration, DataFrame assembly, CLI, map.
  - web.py / notify.py: thin filtering/joining over what the API returns.

No pandas or streamlit types appear in any signature (pandas is used
internally only to keep NaN semantics identical to the pre-facade code).
That constraint is deliberate: an MCP server can wrap these functions
directly, exposing get_schools / resolve_school / get_districts as tools
without an adapter layer.
"""

import os
import sys
import json
import math
import time
import threading
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd

import db
from school_district_lookup import lookup_coords, lookup_attendance_zone
from school_match import names_match
from greatschools_scraper import get_ratings_by_level
from homeharvest import scrape_property


# ---------------------------------------------------------------------------
# Geometry

def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon points."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# External-service plumbing (Nominatim / Overpass)

# Nominatim's usage policy caps clients at ~1 request/second and requires an
# identifying User-Agent. This lock + timestamp enforces that spacing across
# threads so parallel workers can't stampede the public server.
_NOMINATIM_UA = 'school-rental-search/1.0 (github.com/MLS rental+school finder)'
_OVERPASS_UA = _NOMINATIM_UA
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]
_NOMINATIM_MIN_INTERVAL = 1.1  # seconds between Nominatim calls

_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"


def _http_get_json(url: str, timeout: float, user_agent: str):
    """GET a URL and parse JSON, with a hard timeout. Returns None on any failure."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _nominatim_get(url: str, timeout: float = 8.0):
    """Rate-limited Nominatim GET (>=1.1s between calls, process-wide)."""
    with _nominatim_lock:
        wait = _NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last[0])
        if wait > 0:
            time.sleep(wait)
        result = _http_get_json(url, timeout, _NOMINATIM_UA)
        _nominatim_last[0] = time.monotonic()
    return result


def _nominatim_lookup(query_str: str) -> dict:
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={urllib.parse.quote(query_str)}&format=json&limit=1&addressdetails=1")
    data = _nominatim_get(url, timeout=8.0)
    return data[0] if data else {}


def geocode(location: str) -> dict:
    """Resolve a place name to coordinates + state.

    Returns {'lat': float|None, 'lon': float|None, 'state': str|None}."""
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={urllib.parse.quote(location)}&format=json&limit=1&addressdetails=1")
    data = _nominatim_get(url, timeout=8.0)
    if data:
        addr = data[0].get('address', {})
        state = addr.get('ISO3166-2-lvl4', '').replace('US-', '') or ''
        return {'lat': float(data[0]['lat']), 'lon': float(data[0]['lon']),
                'state': state}
    return {'lat': None, 'lon': None, 'state': None}


def _overpass_zip_near(lat: float, lon: float, radius_m: int = 2000) -> str:
    """Find the most common addr:postcode on nodes near a point."""
    query = (f'[out:json][timeout:10];'
             f'node["addr:postcode"](around:{radius_m},{lat},{lon});'
             f'out tags;')
    url = f"{_OVERPASS_ENDPOINT}?data={urllib.parse.quote(query)}"
    result = _http_get_json(url, timeout=12.0, user_agent=_OVERPASS_UA)
    if not result:
        return ''
    codes = [e['tags']['addr:postcode'] for e in result.get('elements', [])
             if 'addr:postcode' in e.get('tags', {})]
    return max(set(codes), key=codes.count) if codes else ''


def town_zip(name: str, lat: float, lon: float, state: str) -> str:
    """Resolve one town to a zip: Overpass postcode nodes first (parallel-safe),
    Nominatim only as a rate-limited fallback. Cached forever-ish (db)."""
    key = f'{name}|{state}'
    cached = db.get('town_zip', key)
    if cached:
        return cached

    code = _overpass_zip_near(lat, lon)
    if not code:
        nom = _nominatim_lookup(f"{name}, {state}")
        code = nom.get('address', {}).get('postcode', '')
    if code:                       # don't cache a failed lookup
        db.put('town_zip', key, code)
    return code


def towns_within(lat: float, lon: float, radius_miles: float,
                 verbose: bool = False) -> list:
    """All town/city nodes within radius, nearest-first, as
    [{'name','lat','lon','distance_mi'}]. One Overpass call with a hard
    timeout; returns [] on failure so the caller falls back to the primary
    city only. Zip resolution is deferred (town_zip) so towns are resolved
    lazily as a search expands outward.

    NOTE: the db 'towns' cache stores the legacy [dist, name, lat, lon] list
    format -- existing cache entries must keep hitting -- so conversion to
    dicts happens only at the return boundary."""
    cache_key = f'{round(lat, 2)},{round(lon, 2)},{radius_miles}'

    def _to_dicts(tuples):
        return [{'name': t[1], 'lat': t[2], 'lon': t[3], 'distance_mi': t[0]}
                for t in tuples]

    cached = db.get('towns', cache_key)
    if cached is not None:
        return _to_dicts([tuple(t) for t in cached])

    radius_m = int(radius_miles * 1609.34)
    query = (f'[out:json][timeout:20];'
             f'(node["place"~"^(city|town)$"](around:{radius_m},{lat},{lon}););'
             f'out body;')
    url = f"{_OVERPASS_ENDPOINT}?data={urllib.parse.quote(query)}"
    result = _http_get_json(url, timeout=25.0, user_agent=_OVERPASS_UA)
    if not result:
        if verbose:
            print("  Overpass town lookup failed/timed out; searching primary city only.")
        return []

    towns = []
    for el in result.get('elements', []):
        name = el.get('tags', {}).get('name', '')
        tlat, tlon = el.get('lat'), el.get('lon')
        if name and tlat is not None and tlon is not None:
            towns.append((haversine_miles(lat, lon, tlat, tlon), name, tlat, tlon))
    towns.sort(key=lambda t: t[0])
    if towns:                      # don't cache a failed/empty Overpass call
        db.put('towns', cache_key, towns)
    return _to_dicts(towns)


# ---------------------------------------------------------------------------
# Listings

def get_listings(location: str, listing_type: str = 'for_rent',
                 past_days: int = 30) -> list:
    """Raw listings for a location as a list of dicts (one per listing).

    Thin wrapper over homeharvest. NaN values are preserved as-is (callers use
    pd.notna, and notify's dedup keys depend on the exact string forms), and
    scrape exceptions propagate so the caller can report a skipped location."""
    raw = scrape_property(location=location, listing_type=listing_type,
                          past_days=past_days)
    if raw is None or raw.empty:
        return []
    return raw.to_dict('records')


# ---------------------------------------------------------------------------
# Schools / districts / zones

def get_schools(lat: float, lon: float, radius_miles: float = 5.0,
                level: str = None, limit: int = 100) -> list:
    """Schools near a point, nearest first, each joined with its rating.

    Rows carry the NCES directory fields plus 'distance_mi', 'rating' and
    'rating_source' (rating None when absent or stale; source='manual' rows
    never expire). The join lives here so callers only filter and display."""
    return db.schools_near(lat, lon, radius_miles, level, limit)


def get_school(ncessch: str):
    """One school (directory + rating) by NCES id, or None."""
    return db.get_school(ncessch)


def get_districts(lat: float, lon: float) -> dict:
    """School district(s) serving a point.

    Returns {'unified': {...}} XOR {'elementary': {...}[, 'secondary': {...}]}
    plus 'error' (None on success). Each district dict has name/geoid/
    low_grade/high_grade; geoid equals the NCES leaid, so it joins directly to
    the schools table."""
    dr = lookup_coords(float(lat), float(lon))
    out = {'error': dr.get('error')}
    for label, info in (dr.get('school_districts') or {}).items():
        out[label] = {
            'name': info.get('name', ''),
            'geoid': info.get('geoid', ''),
            'low_grade': info.get('low_grade', ''),
            'high_grade': info.get('high_grade', ''),
        }
    return out


def get_attendance_zone(lat: float, lon: float, state: str = None) -> dict:
    """Which schools' attendance zones contain this point?

    {'status': 'assigned'|'unzoned'|'unavailable', 'zones': {level: {...}}}.
    Never guesses: 'unzoned' means the district did not participate in SABS,
    and nearest-school substitution is wrong 43.6% of the time."""
    return lookup_attendance_zone(float(lat), float(lon), state)


# ---------------------------------------------------------------------------
# Ratings (area cells + per-address resolution)

class Session:
    """Per-search in-memory memo for area-ratings cells, shared across
    enrichment worker threads. Create one per search run; None means one-off
    (the db-backed cache still applies)."""

    def __init__(self):
        self.ratings_memo = {}
        self.lock = threading.Lock()


def new_session() -> Session:
    return Session()


def get_area_ratings(lat: float, lon: float, session: Session = None) -> dict:
    """GreatSchools ratings for the ~1km cell containing a point.

    Three tiers: session memo (this run) -> sqlite ratings_v2 (across runs)
    -> live scrape. The query uses the cell CENTROID, not the raw coords: the
    cache key is quantized to ~1km, so fetching by raw coords would let two
    addresses in the same cell issue different radius queries and race to
    overwrite one key with different ratings.

    Returns {level: {'rating','count','top_school','top_rating','schools':[...]}}
    with empty dicts per level on scrape failure (failures are never cached)."""
    cell_lat, cell_lon = round(float(lat), 2), round(float(lon), 2)
    cache_key = f'{cell_lat},{cell_lon}'

    r = None
    if session is not None:
        with session.lock:
            r = session.ratings_memo.get(cache_key)
    if r is None:
        r = db.get('ratings_v2', cache_key)
    if r is None:
        # Fetch outside the lock; concurrent duplicate fetches of the same
        # ~1km cell are wasteful but harmless.
        try:
            r = get_ratings_by_level(cell_lat, cell_lon)
            # Only persist real results -- caching a failed scrape would
            # blank out this cell for the whole TTL.
            db.put('ratings_v2', cache_key, r)
        except Exception:
            r = {'elementary': {}, 'middle': {}, 'high': {}}
    if session is not None:
        with session.lock:
            session.ratings_memo[cache_key] = r
    return r


# SABS encodes attendance-zone level as 1/2/3; our vocabulary is by name.
_SABS_LEVEL = {'elementary': ('1', 'primary'), 'middle': ('2', 'middle'),
               'high': ('3', 'high')}


def _rating_for_school(name: str, ncessch: str, ratings: dict):
    """Rating for one specific school.

    Prefers the schools table (exact, keyed by NCES id, populated by
    scripts/backfill_school_ratings.py) and falls back to name-matching within
    this cell's GreatSchools payload. Returns None when the school can't be
    identified confidently -- the caller then keeps the area average and marks
    it unconfirmed rather than attaching another school's number."""
    if ncessch:
        stored = db.get_school_rating(ncessch)
        if stored is not None:
            return stored
    if not name:
        return None
    for level in ('elementary', 'middle', 'high', 'other'):
        for s in (ratings.get(level) or {}).get('schools', []) or []:
            if s.get('rating') is not None and names_match(name, s.get('name', '')):
                return s['rating']
    return None


def _resolve_level(level: str, zones: dict, leaid: str, ratings: dict,
                   area_avg):
    """Best available rating for one school level at one address.

    Returns (rating, best_case, school_name, source, unrated_count).

    Same precedence at every level, so a filter means the same thing whichever
    one the user picks:
      zoned         the address's assigned school, exactly
      district-sole the district has one school at this level, so no ambiguity
      district-min  worst school in the district -- a floor, not a guess
      area-avg      last resort ~3mi average; NOT a bound, since the radius
                    crosses district lines
    """
    school, source, best, unrated = '', 'area-avg', None, 0
    rating = area_avg

    keys = _SABS_LEVEL.get(level, ())
    zone = next((zones[k] for k in keys if k in zones), None) if zones else None
    if zone:
        school = zone.get('school', '')
        assigned = _rating_for_school(school, zone.get('ncessch', ''), ratings)
        if assigned is not None:
            return assigned, None, school, 'zoned', 0
        source = 'zoned-unrated'

    if leaid:
        cands = db.schools_in_district(leaid, level)
        rated = [c for c in cands if c.get('rating') is not None]
        if rated:
            worst = min(c['rating'] for c in rated)
            if len(cands) == 1:
                return worst, worst, cands[0]['name'], 'district-sole', 0
            return (worst, max(c['rating'] for c in rated), school,
                    'district-min', len(cands) - len(rated))
    return rating, best, school, source, unrated


def _source_note(source: str, worst, best, unrated: int) -> str:
    """User-facing caveat for a school rating. Empty when exact."""
    if source in ('zoned', 'district-sole'):
        return ''
    if source == 'district-min':
        rng = f"{worst}-{best}" if best is not None and best != worst else f"{worst}"
        note = f"not exact - worst case ({rng} across district)"
        if unrated:
            return note + f", {unrated} unrated"
        return note
    if source == 'zoned-unrated':
        return 'school known, no rating available'
    return '*confirm elementary - area average only'


def resolve_school(lat: float, lon: float, level: str, zones: dict = None,
                   districts: dict = None, area: dict = None,
                   session: Session = None) -> dict:
    """Which school serves this address at this level, and how sure are we?

    Standalone entry point for one address (MCP-friendly). Fetches whichever
    of zones/districts/area the caller didn't supply, then applies the same
    precedence as listing enrichment. Note the enrichment path calls
    _resolve_level directly with its own already-fetched inputs; this wrapper
    exists for callers outside a search run.

    Returns {'level','rating','best_case','school','source','unrated','note'}.
    """
    if zones is None:
        z = get_attendance_zone(lat, lon)
        zones = z.get('zones') or {} if z.get('status') == 'assigned' else {}
    if districts is None:
        districts = get_districts(lat, lon)
    if area is None:
        area = get_area_ratings(lat, lon, session)

    elem_leaid = (districts.get('unified') or districts.get('elementary') or {}).get('geoid', '')
    if level == 'high':
        leaid = (districts.get('secondary') or {}).get('geoid', '') or elem_leaid
    else:
        leaid = elem_leaid

    area_avg = (area.get(level) or {}).get('rating')
    rating, best, school, source, unrated = _resolve_level(
        level, zones, leaid, area, area_avg)
    return {'level': level, 'rating': rating, 'best_case': best,
            'school': school, 'source': source, 'unrated': unrated,
            'note': _source_note(source, rating, best, unrated)}


# ---------------------------------------------------------------------------
# Listing enrichment

ROOM_KEYWORDS = ['rooming', 'room for rent', 'room rental', 'single room',
                 'not the entire', 'shared', 'room in', 'one room', '1 room']


def enrich_listing(listing: dict, session: Session = None) -> dict:
    """Enrich one raw listing (dict) with warning flags and school data.

    Thread-safe: only the shared session memo is mutated, and only under its
    lock. lookup_coords is lru_cache'd (already thread-safe)."""
    row = listing
    lat, lon = row.get('latitude'), row.get('longitude')
    sqft, price = row.get('sqft'), row.get('list_price')
    days_on_mls = row.get('days_on_mls')
    text = str(row.get('text', '')).lower() if pd.notna(row.get('text')) else ''

    # Build warning flags
    flags = []
    if pd.notna(row.get('unit')) and row.get('unit'):
        flags.append('UNIT')
    if any(kw in text for kw in ROOM_KEYWORDS):
        flags.append('ROOM')
    if pd.notna(sqft) and sqft > 10000:
        flags.append('SQFT?')
    if pd.notna(sqft) and pd.notna(price) and sqft > 0 and price / sqft < 0.50:
        flags.append('PRICE?')
    if pd.notna(days_on_mls) and days_on_mls > 60:
        flags.append(f'OLD({int(days_on_mls)}d)')
    style = str(row.get('style', '')).upper() if pd.notna(row.get('style')) else ''
    if any(x in style for x in ['CONDO', 'DUPLEX', 'MULTI', 'TRIPLEX']):
        flags.append('MULTI')

    # Get school data
    district, district_grades = '', ''
    district_hs, district_hs_grades = '', ''
    elem_school, elem_source = '', 'area-avg'
    mid_school, mid_source = '', 'area-avg'
    high_school_name, high_source = '', 'area-avg'
    elem_best = mid_best = high_best = None
    elem_unrated = mid_unrated = high_unrated = 0
    elem_leaid, hs_leaid = '', ''
    elem, mid, high = None, None, None
    top_school, top_rating = '', None
    school_count = 0

    if pd.notna(lat) and pd.notna(lon):
        try:
            d = get_districts(float(lat), float(lon))
            if not d.get('error'):

                def _fmt(info):
                    return (info.get('name', ''),
                            f"{info.get('low_grade', '?')}-{info.get('high_grade', '?')}")

                # A point is served either by one unified district or by an
                # elementary + secondary pair -- never both (verified: zero
                # overlap across 166 sampled points in 26 states). Surface the
                # secondary district separately so the high-school district
                # isn't dropped, which it was for all 20 scsd states.
                if 'unified' in d:
                    district, district_grades = _fmt(d['unified'])
                    elem_leaid = d['unified'].get('geoid', '')
                else:
                    if 'elementary' in d:
                        district, district_grades = _fmt(d['elementary'])
                        elem_leaid = d['elementary'].get('geoid', '')
                    if 'secondary' in d:
                        district_hs, district_hs_grades = _fmt(d['secondary'])
                        hs_leaid = d['secondary'].get('geoid', '')
        except Exception:
            pass

        r = get_area_ratings(float(lat), float(lon), session)

        elem_data = r.get('elementary', {})
        mid_data = r.get('middle', {})
        high_data = r.get('high', {})

        elem = elem_data.get('rating')
        mid = mid_data.get('rating')
        high = high_data.get('rating')

        # Resolve every level the same way, so whichever level the user
        # filters on carries the same guarantee. See _resolve_level.
        try:
            z = get_attendance_zone(float(lat), float(lon), row.get('state'))
            zones = z.get('zones') or {} if z.get('status') == 'assigned' else {}

            elem, elem_best, elem_school, elem_source, elem_unrated = _resolve_level(
                'elementary', zones, elem_leaid, r, elem)
            mid, mid_best, mid_school, mid_source, mid_unrated = _resolve_level(
                'middle', zones, elem_leaid, r, mid)
            # High school often sits in a separate secondary district; fall back
            # to the unified district when there isn't one.
            high, high_best, high_school_name, high_source, high_unrated = _resolve_level(
                'high', zones, hs_leaid or elem_leaid, r, high)
        except Exception:
            pass

        # Get top school (highest rated across all levels)
        for level_data in [elem_data, mid_data, high_data]:
            if level_data.get('top_rating') and (top_rating is None or level_data['top_rating'] > top_rating):
                top_school = level_data.get('top_school', '')
                top_rating = level_data.get('top_rating')
            school_count += level_data.get('count', 0)

    return {
        'address': row.get('full_street_line', row.get('street', '')),
        'city': row.get('city', ''),
        'state': row.get('state', ''),
        'zip': row.get('zip_code', ''),
        'price': int(price) if pd.notna(price) else None,
        'beds': int(row['beds']) if pd.notna(row.get('beds')) else None,
        'baths': int(row['full_baths']) if pd.notna(row.get('full_baths')) else None,
        'sqft': int(sqft) if pd.notna(sqft) else None,
        'lat': float(lat) if pd.notna(lat) else None,
        'lon': float(lon) if pd.notna(lon) else None,
        'url': row.get('property_url', ''),
        'flags': '|'.join(flags) if flags else '',
        'district': district,
        'district_grades': district_grades,
        'district_hs': district_hs,
        'district_hs_grades': district_hs_grades,
        'elem': elem,
        'elem_school': elem_school,
        'elem_best': elem_best,
        'elem_source': elem_source,
        # Shown verbatim. 'zoned'/'district-sole' are exact; 'district-min' is a
        # guaranteed floor across the district's schools; anything else is a
        # rough area average and should not be trusted without checking.
        'elem_confirm': _source_note(elem_source, elem, elem_best, elem_unrated),
        'mid': mid,
        'mid_school': mid_school,
        'mid_best': mid_best,
        'mid_source': mid_source,
        'mid_confirm': _source_note(mid_source, mid, mid_best, mid_unrated),
        'high': high,
        'high_school': high_school_name,
        'high_best': high_best,
        'high_source': high_source,
        'high_confirm': _source_note(high_source, high, high_best, high_unrated),
        'top_school': top_school,
        'top_rating': top_rating,
        'school_count': school_count,
    }


# ---------------------------------------------------------------------------
# Misc

def get_stats() -> dict:
    """Cache/table statistics for the local store."""
    return db.stats()
