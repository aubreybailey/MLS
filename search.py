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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))

import pandas as pd
import folium
from folium import DivIcon
import warnings
warnings.filterwarnings('ignore')

from homeharvest import scrape_property
from school_district_lookup import lookup_coords
from greatschools_scraper import get_ratings_by_level

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


def geocode_location(location: str):
    """Return (lat, lon, state_abbr) for a location string using Nominatim."""
    import urllib.request, json, urllib.parse
    query = urllib.parse.quote(location)
    url = f"https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=1&addressdetails=1"
    req = urllib.request.Request(url, headers={'User-Agent': 'rental-search/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if data:
        addr = data[0].get('address', {})
        state = addr.get('ISO3166-2-lvl4', '').replace('US-', '') or ''
        return float(data[0]['lat']), float(data[0]['lon']), state
    return None, None, None


def _nominatim_lookup(query_str: str) -> dict:
    import urllib.request, json, urllib.parse
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={urllib.parse.quote(query_str)}&format=json&limit=1&addressdetails=1")
    req = urllib.request.Request(url, headers={'User-Agent': 'rental-search/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    return data[0] if data else {}


def _overpass_zip_near(lat: float, lon: float, radius_m: int = 2000) -> str:
    """Find the most common addr:postcode on nodes near a point."""
    import urllib.request, json, urllib.parse
    try:
        query = (f'[out:json][timeout:15];'
                 f'node["addr:postcode"](around:{radius_m},{lat},{lon});'
                 f'out tags;')
        url = f"https://overpass-api.de/api/interpreter?data={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'rental-search/1.0'})
        with urllib.request.urlopen(req, timeout=20) as r:
            els = json.loads(r.read()).get('elements', [])
        codes = [e['tags']['addr:postcode'] for e in els if 'addr:postcode' in e.get('tags', {})]
        if codes:
            return max(set(codes), key=codes.count)
    except Exception:
        pass
    return ''


def get_nearby_zipcodes(lat: float, lon: float, radius_miles: float, state: str) -> list:
    """Return unique zip codes for all cities/towns within radius."""
    import urllib.request, json, urllib.parse
    radius_m = int(radius_miles * 1609.34)
    # Get nearby town/city nodes from Overpass
    query = (f'[out:json][timeout:25];'
             f'(node["place"~"^(city|town)$"](around:{radius_m},{lat},{lon}););'
             f'out body;')
    url = f"https://overpass-api.de/api/interpreter?data={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'rental-search/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())

    seen = set()
    zips = []
    for el in result.get('elements', []):
        name = el.get('tags', {}).get('name', '')
        tlat, tlon = el.get('lat'), el.get('lon')
        if not name or not tlat:
            continue
        # Try Nominatim first for the zip
        nom = _nominatim_lookup(f"{name}, {state}")
        code = nom.get('address', {}).get('postcode', '')
        # Fallback: find nearest addr:postcode node via Overpass
        if not code and tlat:
            code = _overpass_zip_near(tlat, tlon)
        if code and code not in seen:
            seen.add(code)
            zips.append(code)
    return zips


def search_and_enrich(location: str, limit: int = 50, min_beds: int = None,
                      max_price: int = None, radius_miles: float = None,
                      verbose: bool = True) -> pd.DataFrame:
    """Search rentals and enrich with school ratings and warning flags."""
    center_lat, center_lon, state = None, None, None

    # For radius searches: geocode center, find nearby zip codes, search each
    locations_to_search = [location]
    if radius_miles:
        center_lat, center_lon, state = geocode_location(location)
        if center_lat is not None:
            zipcodes = get_nearby_zipcodes(center_lat, center_lon, radius_miles, state)
            locations_to_search.extend(zipcodes)
            if verbose:
                print(f"Searching {location} + {len(zipcodes)} zip codes within {radius_miles} miles")
        else:
            if verbose:
                print(f"Searching rentals in {location}...")
    else:
        if verbose:
            print(f"Searching rentals in {location}...")

    all_dfs = []
    for loc in locations_to_search:
        try:
            df = scrape_property(location=loc, listing_type='for_rent', past_days=30)
            if not df.empty:
                all_dfs.append(df)
                if verbose and radius_miles:
                    print(f"  {loc}: {len(df)} listings")
        except Exception as e:
            if verbose:
                print(f"  {loc}: skipped ({e})")

    if not all_dfs:
        print("No listings found.")
        return pd.DataFrame()

    raw_df = pd.concat(all_dfs, ignore_index=True)
    if 'full_street_line' in raw_df.columns and 'city' in raw_df.columns:
        raw_df = raw_df.drop_duplicates(subset=['full_street_line', 'city'])

    if verbose:
        print(f"Found {len(raw_df)} total listings")

    if min_beds:
        raw_df = raw_df[raw_df['beds'] >= min_beds]
    if max_price:
        raw_df = raw_df[raw_df['list_price'] <= max_price]

    if radius_miles and center_lat is not None:
        mask = raw_df.apply(
            lambda r: pd.notna(r.get('latitude')) and pd.notna(r.get('longitude')) and
                      haversine_miles(center_lat, center_lon, float(r['latitude']), float(r['longitude'])) <= radius_miles,
            axis=1
        )
        before = len(raw_df)
        raw_df = raw_df[mask]
        if verbose:
            print(f"Kept {len(raw_df)} listings within {radius_miles} miles (dropped {before - len(raw_df)} outside radius)")

    # For radius searches enrich all filtered listings, then limit at the end
    # to avoid cutting off suburb listings that appear after the primary city
    if not radius_miles:
        raw_df = raw_df.head(limit)

    if verbose:
        print(f"Processing {len(raw_df)} listings after filters...")

    ratings_cache = {}
    results = []

    for idx, (_, row) in enumerate(raw_df.iterrows()):
        if verbose and idx > 0 and idx % 10 == 0:
            print(f"  Processing {idx}/{len(raw_df)}...")

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
        elem, mid, high = None, None, None
        top_school, top_rating = '', None
        school_count = 0

        if pd.notna(lat) and pd.notna(lon):
            try:
                dr = lookup_coords(float(lat), float(lon))
                if dr and not dr.get('error'):
                    d = dr.get('school_districts', {})
                    if 'unified' in d:
                        dist_info = d['unified']
                        district = dist_info.get('name', '')
                        district_grades = f"{dist_info.get('low_grade', '?')}-{dist_info.get('high_grade', '?')}"
                    elif 'elementary' in d:
                        dist_info = d['elementary']
                        district = dist_info.get('name', '')
                        district_grades = f"{dist_info.get('low_grade', '?')}-{dist_info.get('high_grade', '?')}"
            except Exception:
                pass

            cache_key = f'{round(float(lat), 2)},{round(float(lon), 2)}'
            if cache_key not in ratings_cache:
                try:
                    ratings_cache[cache_key] = get_ratings_by_level(float(lat), float(lon))
                except Exception:
                    ratings_cache[cache_key] = {'elementary': {}, 'middle': {}, 'high': {}}

            r = ratings_cache[cache_key]
            elem_data = r.get('elementary', {})
            mid_data = r.get('middle', {})
            high_data = r.get('high', {})

            elem = elem_data.get('rating')
            mid = mid_data.get('rating')
            high = high_data.get('rating')

            # Get top school (highest rated across all levels)
            for level_data in [elem_data, mid_data, high_data]:
                if level_data.get('top_rating') and (top_rating is None or level_data['top_rating'] > top_rating):
                    top_school = level_data.get('top_school', '')
                    top_rating = level_data.get('top_rating')
                school_count += level_data.get('count', 0)

        results.append({
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
            'elem': elem,
            'mid': mid,
            'high': high,
            'top_school': top_school,
            'top_rating': top_rating,
            'school_count': school_count,
        })

    df = pd.DataFrame(results)
    if radius_miles:
        # Sort suburb listings before Worcester so head(limit) gets a true cross-section;
        # use school rating desc as tiebreaker so best listings rise to the top
        df = df.sort_values('elem', ascending=False, na_position='last').head(limit)
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
        """
    )
    parser.add_argument("location", help="City/area to search (e.g., 'Providence, RI')")
    parser.add_argument("--output", "-o", help="Output filename base (without extension)")
    parser.add_argument("--limit", "-n", type=int, default=50, help="Max listings (default: 50)")
    parser.add_argument("--min-beds", type=int, help="Minimum bedrooms")
    parser.add_argument("--max-price", type=int, help="Maximum monthly rent")
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
        radius_miles=args.radius,
        verbose=not args.quiet
    )

    if df.empty:
        return 1

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
