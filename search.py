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
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import folium
from folium import DivIcon
import warnings
warnings.filterwarnings('ignore')

# All data access (db, GPKG lookups, GreatSchools, Nominatim/Overpass,
# homeharvest listings) goes through the api facade. This module is
# orchestration only: quota-fill, DataFrame assembly, CLI, map.
import api
from api import haversine_miles


# The radius search expands town-by-town (nearest first) only until the hit
# quota is filled, so there is no fixed town count. This is just a generous
# safety ceiling so a strict filter over a dense metro can't try to scrape an
# unbounded number of towns (and get the scraper IP-blocked); normal searches
# fill their quota long before reaching it.
MAX_TOWNS_EXPAND = 60
# Which record field each user-selectable school level filters on.
SCHOOL_LEVELS = {'elementary': 'elem', 'middle': 'mid', 'high': 'high'}


def get_color(rating):
    """Get marker color based on school rating."""
    if pd.isna(rating): return '#888'
    if rating >= 8: return '#228B22'
    elif rating >= 7: return '#32CD32'
    elif rating >= 6: return '#FFA500'
    elif rating >= 5: return '#FF6347'
    else: return '#DC143C'


def _passes(rec: dict, min_rating, hide_flagged: bool, hide_units: bool,
            school_level: str = 'elementary', min_sqft=None) -> bool:
    """Does an enriched listing count as a hit under the scan filters?"""
    if min_rating is not None:
        field = SCHOOL_LEVELS.get(school_level, 'elem')
        value = rec.get(field)
        if value is None or value < min_rating:
            return False
    if min_sqft is not None and (rec.get('sqft') is None or rec['sqft'] < min_sqft):
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
                      max_price: int = None, min_rating: float = None,
                      school_level: str = 'elementary', min_sqft: int = None,
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
        g = api.geocode(location)
        center_lat, center_lon, state = g['lat'], g['lon'], g['state']
        if center_lat is not None:
            towns = api.towns_within(center_lat, center_lon, radius_miles, verbose)[:MAX_TOWNS_EXPAND]
            if verbose:
                print(f"Searching {location} + up to {len(towns)} towns within "
                      f"{radius_miles} mi, expanding until {limit} hits...")
        elif verbose:
            print(f"Searching rentals in {location}...")
    elif verbose:
        print(f"Searching rentals in {location}...")

    seen = set()                       # dedup listings across the city + overlapping zips
    session = api.new_session()        # per-run memo for area-ratings cells
    hits, scanned, pool, towns_used = [], 0, 0, 0

    def _location_stream():
        """Primary city first, then towns nearest->farthest. Each town's zip is
        resolved lazily (only once we reach it), falling back to 'Town, ST'."""
        yield location, None
        for t in towns:
            code = api.town_zip(t['name'], t['lat'], t['lon'], state)
            yield (code or f"{t['name']}, {state}"), t['distance_mi']

    def _prefilter(raw):
        """Yield rows passing the cheap pre-enrichment filters (beds/price/radius
        mask), deduped, so school enrichment is only spent on real candidates."""
        for row in raw:
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
                raw = api.get_listings(loc)
            except Exception as e:
                if verbose:
                    print(f"  {loc}: skipped ({e})")
                continue
            if not raw:
                continue
            rows = list(_prefilter(raw))
            pool += len(rows)
            for chunk in _chunks(rows, max_workers):
                if len(hits) >= limit:
                    break
                enriched = list(ex.map(
                    lambda r: api.enrich_listing(r, session), chunk))
                for rec in enriched:
                    scanned += 1
                    if not _passes(rec, min_rating, hide_flagged, hide_units,
                                   school_level, min_sqft):
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
    python search.py "Northborough, MA" --min-rating 7 --hide-units --limit 20
    python search.py "Boston, MA" --school-level high --min-rating 8 --min-sqft 1000
        """
    )
    parser.add_argument("location", help="City/area to search (e.g., 'Providence, RI')")
    parser.add_argument("--output", "-o", help="Output filename base (without extension)")
    parser.add_argument("--limit", "-n", type=int, default=50,
                        help="Target number of listings that pass the filters (default: 50)")
    parser.add_argument("--min-beds", type=int, help="Minimum bedrooms")
    parser.add_argument("--max-price", type=int, help="Maximum monthly rent")
    parser.add_argument("--min-rating", type=float,
                        help="Minimum school rating for the chosen --school-level; "
                             "listings below it (or without a rating) are skipped")
    parser.add_argument("--school-level", default="elementary",
                        choices=["elementary", "middle", "high"],
                        help="Which school level --min-rating applies to (default: elementary)")
    parser.add_argument("--min-sqft", type=int, help="Minimum square footage")
    parser.add_argument("--min-elem", type=float, dest="min_rating",
                        help=argparse.SUPPRESS)   # back-compat alias
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
        min_rating=args.min_rating,
        school_level=args.school_level,
        min_sqft=args.min_sqft or None,
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
