#!/usr/bin/env python3
"""
Standalone CLI for rental search with school ratings and map output.

Usage:
    python search.py "Providence, RI"
    python search.py "Providence, RI" --output providence
    python search.py "Austin, TX" --limit 50 --min-beds 3 --max-price 3000
"""

import argparse
import sys
import os
import math
import time
import threading
import urllib.request
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))

import pandas as pd
import folium
from folium import DivIcon
import warnings
warnings.filterwarnings('ignore')

from homeharvest import scrape_property
from school_district_lookup import lookup_coords, lookup_attendance_zone
from greatschools_scraper import get_ratings_by_level
import db

def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon points."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


ROOM_KEYWORDS = ['rooming', 'room for rent', 'room rental', 'single room',
                 'not the entire', 'shared', 'room in', 'one room', '1 room']


def get_color(rating):
    """Get marker color based on school rating."""
    if pd.isna(rating): return '#888'
    if rating >= 8: return '#228B22'
    elif rating >= 7: return '#32CD32'
    elif rating >= 6: return '#FFA500'
    elif rating >= 5: return '#FF6347'
    else: return '#DC143C'


# Nominatim's usage policy caps clients at ~1 request/second and requires an
# identifying User-Agent. This lock + timestamp enforces that spacing across
# threads so parallel workers can't stampede the public server.
_NOMINATIM_UA = 'school-rental-search/1.0 (github.com/MLS rental+school finder)'
_OVERPASS_UA = _NOMINATIM_UA
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]
_NOMINATIM_MIN_INTERVAL = 1.1  # seconds between Nominatim calls


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


def geocode_location(location: str):
    """Return (lat, lon, state_abbr) for a location string using Nominatim."""
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={urllib.parse.quote(location)}&format=json&limit=1&addressdetails=1")
    data = _nominatim_get(url, timeout=8.0)
    if data:
        addr = data[0].get('address', {})
        state = addr.get('ISO3166-2-lvl4', '').replace('US-', '') or ''
        return float(data[0]['lat']), float(data[0]['lon']), state
    return None, None, None


def _nominatim_lookup(query_str: str) -> dict:
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={urllib.parse.quote(query_str)}&format=json&limit=1&addressdetails=1")
    data = _nominatim_get(url, timeout=8.0)
    return data[0] if data else {}


# The radius search expands town-by-town (nearest first) only until the hit
# quota is filled, so there is no fixed town count. This is just a generous
# safety ceiling so a strict filter over a dense metro can't try to scrape an
# unbounded number of towns (and get the scraper IP-blocked); normal searches
# fill their quota long before reaching it.
MAX_TOWNS_EXPAND = 60
_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"


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


def _resolve_town_zip(name: str, tlat: float, tlon: float, state: str) -> str:
    """Resolve one town to a zip: Overpass postcode nodes first (parallel-safe),
    Nominatim only as a rate-limited fallback."""
    key = f'{name}|{state}'
    cached = db.get('town_zip', key)
    if cached:
        return cached

    code = _overpass_zip_near(tlat, tlon)
    if not code:
        nom = _nominatim_lookup(f"{name}, {state}")
        code = nom.get('address', {}).get('postcode', '')
    if code:                       # don't cache a failed lookup
        db.put('town_zip', key, code)
    return code


def _towns_within(lat: float, lon: float, radius_miles: float,
                  verbose: bool = False) -> list:
    """All town/city nodes within radius as (dist_miles, name, tlat, tlon),
    sorted nearest-first. One Overpass call with a hard timeout; returns [] on
    failure so the caller falls back to the primary city only. Zip resolution is
    deferred to the caller, which resolves towns lazily as it expands outward."""
    cache_key = f'{round(lat, 2)},{round(lon, 2)},{radius_miles}'
    cached = db.get('towns', cache_key)
    if cached is not None:
        return [tuple(t) for t in cached]     # JSON round-trips tuples to lists

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
    return towns


_SCHOOL_NOISE = ('elementary', 'school', 'middle', 'high', 'academy', 'the',
                 'jr', 'sr', 'junior', 'senior')


def _norm_school(name: str) -> set:
    """Tokenize a school name for matching NCES names against GreatSchools ones
    (e.g. 'Marion E Zeh' vs 'Marion E. Zeh Elementary School')."""
    cleaned = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in str(name).lower())
    return {t for t in cleaned.split() if t and t not in _SCHOOL_NOISE}


def _rating_for_school(name: str, ratings: dict):
    """Find the GreatSchools rating for one specific school, across all levels.

    Returns None when we can't confidently identify it -- the caller then falls
    back to the area average and marks the result as unconfirmed rather than
    attaching a number to the wrong school."""
    want = _norm_school(name)
    if not want:
        return None
    best, best_overlap = None, 0
    for level in ('elementary', 'middle', 'high', 'other'):
        for s in (ratings.get(level) or {}).get('schools', []) or []:
            got = _norm_school(s.get('name', ''))
            if not got:
                continue
            overlap = len(want & got)
            # Require the shorter name to be essentially contained in the other,
            # so 'Lincoln Street' doesn't match 'Lincoln High'.
            if overlap >= min(len(want), len(got)) and overlap > best_overlap:
                best, best_overlap = s.get('rating'), overlap
    return best


def _enrich_row(row, ratings_cache: dict, cache_lock: threading.Lock) -> dict:
    """Enrich one raw listing row with warning flags and school data.

    Thread-safe: only the shared ratings_cache is mutated, and only under
    cache_lock. lookup_coords is lru_cache'd (already thread-safe)."""
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
    elem, mid, high = None, None, None
    top_school, top_rating = '', None
    school_count = 0

    if pd.notna(lat) and pd.notna(lon):
        try:
            dr = lookup_coords(float(lat), float(lon))
            if dr and not dr.get('error'):
                d = dr.get('school_districts', {})

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
                else:
                    if 'elementary' in d:
                        district, district_grades = _fmt(d['elementary'])
                    if 'secondary' in d:
                        district_hs, district_hs_grades = _fmt(d['secondary'])
        except Exception:
            pass

        # Query the cell centroid, not the listing's raw coords: the cache key is
        # quantized to ~1km, so fetching by raw coords would let two listings in
        # the same cell issue different radius queries and race to overwrite one
        # key with different ratings. Centroid keeps key and value consistent.
        cell_lat, cell_lon = round(float(lat), 2), round(float(lon), 2)
        cache_key = f'{cell_lat},{cell_lon}'
        # Three tiers: in-memory (this run) -> sqlite (across runs) -> scrape.
        with cache_lock:
            r = ratings_cache.get(cache_key)
        if r is None:
            r = db.get('ratings_v2', cache_key)
        if r is None:
            # Fetch outside the lock; concurrent duplicate fetches of the same
            # ~1km cell are wasteful but harmless.
            try:
                r = get_ratings_by_level(cell_lat, cell_lon)
                # Only persist real results — caching a failed scrape would
                # blank out this cell for the whole TTL.
                db.put('ratings_v2', cache_key, r)
            except Exception:
                r = {'elementary': {}, 'middle': {}, 'high': {}}
        with cache_lock:
            ratings_cache[cache_key] = r

        elem_data = r.get('elementary', {})
        mid_data = r.get('middle', {})
        high_data = r.get('high', {})

        elem = elem_data.get('rating')
        mid = mid_data.get('rating')
        high = high_data.get('rating')

        # Attendance zone: which elementary does this address ACTUALLY feed
        # into? Only SABS can answer that; nearest-school is wrong 43.6% of the
        # time, so when there's no zone we keep the area average but mark it
        # unconfirmed instead of implying we know the school.
        try:
            z = lookup_attendance_zone(float(lat), float(lon), row.get('state'))
            if z.get('status') == 'assigned':
                # SABS 'level' 1 = primary/elementary.
                zone = z['zones'].get('1') or z['zones'].get('primary')
                if zone:
                    elem_school = zone.get('school', '')
                    assigned = _rating_for_school(elem_school, r)
                    if assigned is not None:
                        elem = assigned          # this school, not an average
                        elem_source = 'zoned'
                    else:
                        # We know the school but GreatSchools didn't return it.
                        elem_source = 'zoned-unrated'
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
        'elem_source': elem_source,
        # Shown to the user verbatim: 'zoned' means we know the assigned school,
        # anything else means go check before trusting the rating.
        'elem_confirm': '' if elem_source == 'zoned' else '*confirm elementary',
        'mid': mid,
        'high': high,
        'top_school': top_school,
        'top_rating': top_rating,
        'school_count': school_count,
    }


def _passes(rec: dict, min_elem, hide_flagged: bool, hide_units: bool) -> bool:
    """Does an enriched listing count as a hit under the scan filters?"""
    if min_elem is not None and (rec['elem'] is None or rec['elem'] < min_elem):
        return False
    if hide_flagged and rec['flags']:
        return False
    if hide_units and 'UNIT' in rec['flags']:
        return False
    return True


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def search_and_enrich(location: str, limit: int = 50, min_beds: int = None,
                      max_price: int = None, min_elem: float = None,
                      hide_flagged: bool = False, hide_units: bool = False,
                      radius_miles: float = None, max_workers: int = 8,
                      progress_cb=None, verbose: bool = True) -> pd.DataFrame:
    """Search rentals and enrich with school ratings and warning flags.

    `limit` is a quota of listings that PASS the filters. Listings are enriched
    in discovery order and non-matches discarded until `limit` hits are collected
    or the search is exhausted. For radius searches the town count is NOT fixed up
    front: towns within the radius are streamed in nearest-first and the search
    expands to more towns only until the quota is filled (or the radius runs out).
    Scan stats are attached as df.attrs (scanned/pool/limit/matched/towns_used)
    so callers can report shortfalls."""
    center_lat = center_lon = state = None
    towns = []
    if radius_miles:
        center_lat, center_lon, state = geocode_location(location)
        if center_lat is not None:
            towns = _towns_within(center_lat, center_lon, radius_miles, verbose)[:MAX_TOWNS_EXPAND]
            if verbose:
                print(f"Searching {location} + up to {len(towns)} towns within "
                      f"{radius_miles} mi, expanding until {limit} hits...")
        elif verbose:
            print(f"Searching rentals in {location}...")
    elif verbose:
        print(f"Searching rentals in {location}...")

    seen = set()                       # dedup listings across the city + overlapping zips
    ratings_cache, cache_lock = {}, threading.Lock()
    hits, scanned, pool, towns_used = [], 0, 0, 0

    def _location_stream():
        """Primary city first, then towns nearest->farthest. Each town's zip is
        resolved lazily (only once we reach it), falling back to 'Town, ST'."""
        yield location, None
        for dist, name, tlat, tlon in towns:
            code = _resolve_town_zip(name, tlat, tlon, state)
            yield (code or f"{name}, {state}"), dist

    def _prefilter(raw):
        """Yield rows passing the cheap pre-enrichment filters (beds/price/radius
        mask), deduped, so school enrichment is only spent on real candidates."""
        for _, row in raw.iterrows():
            key = (row.get('full_street_line', row.get('street', '')), row.get('city', ''))
            if key in seen:
                continue
            seen.add(key)
            if min_beds and not (pd.notna(row.get('beds')) and row['beds'] >= min_beds):
                continue
            if max_price and not (pd.notna(row.get('list_price')) and row['list_price'] <= max_price):
                continue
            if radius_miles and center_lat is not None:
                la, lo = row.get('latitude'), row.get('longitude')
                if not (pd.notna(la) and pd.notna(lo) and
                        haversine_miles(center_lat, center_lon, float(la), float(lo)) <= radius_miles):
                    continue
            yield row

    # Expand outward one location at a time; enrich each location's candidates in
    # parallel chunks (quota-fill, overshoot bounded to a chunk) and stop the whole
    # search as soon as `limit` hits are collected.
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for loc, dist in _location_stream():
            if len(hits) >= limit:
                break
            if dist is not None:
                towns_used += 1
            try:
                raw = scrape_property(location=loc, listing_type='for_rent', past_days=30)
            except Exception as e:
                if verbose:
                    print(f"  {loc}: skipped ({e})")
                continue
            if raw is None or raw.empty:
                continue
            rows = list(_prefilter(raw))
            pool += len(rows)
            for chunk in _chunks(rows, max_workers):
                if len(hits) >= limit:
                    break
                enriched = list(ex.map(
                    lambda r: _enrich_row(r, ratings_cache, cache_lock), chunk))
                for rec in enriched:
                    scanned += 1
                    if not _passes(rec, min_elem, hide_flagged, hide_units):
                        continue
                    hits.append(rec)
                    if len(hits) >= limit:
                        break
                if progress_cb:
                    # (hits so far, target, scanned so far). Guarded so a UI
                    # callback hiccup can't take down the search.
                    try:
                        progress_cb(len(hits), limit, scanned)
                    except Exception:
                        pass
            if verbose:
                where = location if dist is None else f"{loc} ({dist:.0f}mi)"
                print(f"  after {where}: {len(hits)}/{limit} hits (scanned {scanned})")

    if not seen:
        print("No listings found.")

    df = pd.DataFrame(hits)
    if not df.empty:
        # Hits are collected in discovery order; sort only for display.
        df = df.sort_values('elem', ascending=False, na_position='last')
    df.attrs.update(scanned=scanned, pool=pool, limit=limit, matched=len(df),
                    towns_used=towns_used)
    return df


def create_map(df: pd.DataFrame, title: str = "Rental Search") -> folium.Map:
    """Create Folium map with color-coded markers."""
    valid_coords = df.dropna(subset=['lat', 'lon'])
    if valid_coords.empty:
        center = (42.36, -71.06)
    else:
        center = (valid_coords['lat'].mean(), valid_coords['lon'].mean())

    m = folium.Map(location=center, zoom_start=11, tiles='OpenStreetMap')

    title_html = f'''
        <div style="position: fixed; top: 10px; left: 50px; z-index: 1000;
                    background: white; padding: 10px; border-radius: 5px;
                    box-shadow: 2px 2px 5px rgba(0,0,0,0.3);">
            <b>{title}</b> | {len(df)} listings
        </div>
    '''
    m.get_root().html.add_child(folium.Element(title_html))

    legend_html = '''
        <div style="position: fixed; bottom: 30px; right: 30px; z-index: 1000;
                    background: white; padding: 10px; border-radius: 5px;
                    box-shadow: 2px 2px 5px rgba(0,0,0,0.3); font-size: 12px;">
            <b>School Rating</b><br>
            <span style="color: #228B22;">&#9679;</span> 8+ Excellent<br>
            <span style="color: #32CD32;">&#9679;</span> 7-8 Good<br>
            <span style="color: #FFA500;">&#9679;</span> 6-7 Average<br>
            <span style="color: #FF6347;">&#9679;</span> 5-6 Below Avg<br>
            <span style="color: #DC143C;">&#9679;</span> &lt;5 Poor<br>
            <span style="color: #888;">&#9679;</span> No data<br>
            <b>---</b><br>
            Dashed = Has flags
        </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))

    for _, row in df.iterrows():
        if pd.isna(row['lat']) or pd.isna(row['lon']):
            continue

        price_k = f"{row['price']/1000:.1f}" if row['price'] else '?'
        color = get_color(row['elem'])

        if row['flags']:
            border_style = "2px dashed #666"
            opacity = 0.6
        else:
            border_style = "2px solid white"
            opacity = 0.9

        # Build top school display
        top_school_html = ''
        if row.get('top_school') and row.get('top_rating'):
            top_school_html = f'<b>Top School:</b> {row["top_school"]} ({row["top_rating"]}/10)<br>'

        popup_html = f'''
        <div style="min-width: 220px;">
            <b>{row["address"]}</b><br>
            {row["city"]}, {row["state"]} {row["zip"]}<br>
            <hr style="margin: 5px 0;">
            <b>${row["price"]:,}/mo</b> | {row["beds"]}bd/{row["baths"]}ba | {row["sqft"] or "?"} sqft<br>
            <hr style="margin: 5px 0;">
            <b>Schools ({row.get("school_count", 0)} nearby):</b><br>
            Elementary: {row["elem"] or "N/A"}/10<br>
            Middle: {row["mid"] or "N/A"}/10<br>
            High: {row["high"] or "N/A"}/10<br>
            {top_school_html}District: {row["district"] or "Unknown"} {f'({row["district_grades"]})' if row.get("district_grades") else ''}<br>
            {f'HS District: {row["district_hs"]} ({row["district_hs_grades"]})<br>' if row.get("district_hs") else ''}
            {f'<hr style="margin: 5px 0;"><b style="color:red;">&#9888; {row["flags"]}</b><br>' if row["flags"] else ''}
            <hr style="margin: 5px 0;">
            <a href="{row["url"]}" target="_blank">View Listing</a>
        </div>
        '''

        icon_html = f'''
        <div style="
            background-color: {color};
            border: {border_style};
            border-radius: 50%;
            width: 32px;
            height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 10px;
            font-weight: bold;
            color: white;
            text-shadow: 1px 1px 1px black;
            box-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            opacity: {opacity};
        ">{price_k}</div>
        '''

        folium.Marker(
            location=[row['lat'], row['lon']],
            icon=DivIcon(html=icon_html, icon_size=(32, 32), icon_anchor=(16, 16)),
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"${row['price']:,} | {row['beds']}bd | Elem:{row['elem'] or '?'}"
        ).add_to(m)

    return m


def print_summary(df: pd.DataFrame):
    """Print a text summary of results."""
    print(f"\n{'='*70}")
    scanned, pool = df.attrs.get('scanned'), df.attrs.get('pool')
    if scanned is not None:
        print(f"RESULTS: {len(df)} hits (scanned {scanned} of {pool} listings)")
    else:
        print(f"RESULTS: {len(df)} listings")
    print(f"{'='*70}\n")

    with_price = df[df['price'].notna()]
    clean = df[df['flags'] == '']

    print(f"Price range: ${with_price['price'].min():,.0f} - ${with_price['price'].max():,.0f}")
    print(f"Clean listings (no flags): {len(clean)}/{len(df)}")
    if df['elem'].notna().any():
        print(f"Avg elementary rating: {df['elem'].mean():.1f}/10")
    print()

    print("TOP 10 BY ELEMENTARY SCHOOL RATING:")
    print("-" * 70)
    top = df.nlargest(10, 'elem', keep='first')
    for _, r in top.iterrows():
        flags_str = f" ⚠️{r['flags']}" if r['flags'] else ""
        price_str = f"${r['price']:>6,}" if pd.notna(r['price']) else "    N/A"
        beds_str = f"{r['beds']}bd" if pd.notna(r['beds']) else "?bd"
        elem_str = f"{r['elem']:>4}" if pd.notna(r['elem']) else "   ?"
        print(f"  {price_str}  {beds_str}  Elem:{elem_str}  {r['address'][:35]:<35}{flags_str}")


def main():
    parser = argparse.ArgumentParser(
        description="Search rentals with school ratings and generate map",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python search.py "Providence, RI"
    python search.py "Providence, RI" --output providence_rentals
    python search.py "Austin, TX" --limit 100 --min-beds 3
    python search.py "Seattle, WA" --max-price 3500 --tsv
    python search.py "Northborough, MA" --min-elem 7 --hide-units --limit 20
        """
    )
    parser.add_argument("location", help="City/area to search (e.g., 'Providence, RI')")
    parser.add_argument("--output", "-o", help="Output filename base (without extension)")
    parser.add_argument("--limit", "-n", type=int, default=50,
                        help="Target number of listings that pass the filters (default: 50)")
    parser.add_argument("--min-beds", type=int, help="Minimum bedrooms")
    parser.add_argument("--max-price", type=int, help="Maximum monthly rent")
    parser.add_argument("--min-elem", type=float, help="Minimum elementary school rating; listings below (or without a rating) are skipped")
    parser.add_argument("--hide-flagged", action="store_true", help="Skip listings with any warning flags")
    parser.add_argument("--hide-units", action="store_true", help="Skip UNIT (apartment) listings")
    parser.add_argument("--radius", type=float, help="Filter to listings within this many miles of the location center")
    parser.add_argument("--tsv", action="store_true", help="Output TSV instead of CSV")
    parser.add_argument("--no-map", action="store_true", help="Skip map generation")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress output")

    args = parser.parse_args()

    df = search_and_enrich(
        location=args.location,
        limit=args.limit,
        min_beds=args.min_beds,
        max_price=args.max_price,
        min_elem=args.min_elem,
        hide_flagged=args.hide_flagged,
        hide_units=args.hide_units,
        radius_miles=args.radius,
        verbose=not args.quiet
    )

    if df.empty:
        print(f"\n0 listings matched your filters (scanned {df.attrs.get('scanned', 0)}).")
        return 0

    target = df.attrs.get('limit', args.limit)
    if len(df) < target:
        print(f"\nFound {len(df)} of {target} requested — pool exhausted after scanning {df.attrs.get('scanned', '?')} listings.")

    if args.output:
        base = args.output
    else:
        base = args.location.lower().replace(',', '').replace(' ', '_') + '_rentals'

    ext = 'tsv' if args.tsv else 'csv'
    sep = '\t' if args.tsv else ','
    data_file = f"{base}.{ext}"
    df.to_csv(data_file, sep=sep, index=False)
    print(f"\nSaved {len(df)} listings to {data_file}")

    if not args.no_map:
        map_file = f"{base}.html"
        m = create_map(df, title=f"Rentals: {args.location}")
        m.save(map_file)
        print(f"Saved map to {map_file}")
        print(f"\nOpen in browser: file://{os.path.abspath(map_file)}")

    if not args.quiet:
        print_summary(df)

    return 0


if __name__ == "__main__":
    sys.exit(main())
