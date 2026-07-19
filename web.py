#!/usr/bin/env python3
"""
School-Aware Rental Search Web App (Streamlit)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))

import streamlit as st
import pandas as pd
import folium
from folium import DivIcon
from streamlit_folium import st_folium
import warnings
warnings.filterwarnings('ignore')

from search import search_and_enrich
from notify import add_saved_search, slugify, SAVED_SEARCHES_PATH

try:
    from streamlit_searchbox import st_searchbox
    _HAS_SEARCHBOX = True
except Exception:
    _HAS_SEARCHBOX = False

st.set_page_config(page_title="Rental Search", layout="wide")

CITIES_CSV = os.path.join(os.path.dirname(__file__), "us_cities.csv")


@st.cache_data
def load_cities() -> list[str]:
    """Load bundled US places (Census Gazetteer) as sorted 'City, ST' strings."""
    try:
        cdf = pd.read_csv(CITIES_CSV, dtype=str)
    except Exception:
        return []
    labels = (cdf["city"] + ", " + cdf["state"]).dropna().unique().tolist()
    labels.sort()
    return labels


def search_cities(term: str) -> list[str]:
    """Search-as-you-type callback: prefix matches first, then substring. Capped."""
    cities = load_cities()
    if not term:
        return cities[:50]
    t = term.lower()
    prefix = [c for c in cities if c.lower().startswith(t)]
    if len(prefix) < 50:
        substr = [c for c in cities if t in c.lower() and c not in prefix]
        prefix.extend(substr)
    return prefix[:50]


@st.cache_data(ttl=3600)
def fetch_listings(location: str, limit: int, radius_miles: float,
                   min_beds: int, max_price: int, min_rating: float,
                   school_level: str, min_sqft: int,
                   hide_flagged: bool, hide_units: bool) -> pd.DataFrame:
    """Fetch and annotate listings via search_and_enrich. All filter params are
    part of the cache key because they change which listings are returned."""
    progress = st.progress(0, text=f"Searching {location}…")

    def _on_progress(hits, target, scanned):
        frac = min(1.0, hits / target) if target else 0.0
        filtered = max(0, scanned - hits)
        progress.progress(
            frac,
            text=f"Found {hits}/{target} hits · scanned {scanned} · {filtered} filtered out",
        )

    try:
        df = search_and_enrich(location=location, limit=limit,
                               min_beds=min_beds, max_price=max_price,
                               min_rating=min_rating, school_level=school_level,
                               min_sqft=min_sqft, hide_flagged=hide_flagged,
                               hide_units=hide_units,
                               radius_miles=radius_miles,
                               progress_cb=_on_progress, verbose=False)
    except Exception as e:
        st.error(f"Error fetching listings: {e}")
        return pd.DataFrame()
    finally:
        progress.empty()
    return df


def get_color(rating):
    if pd.isna(rating): return '#888'
    if rating >= 8: return '#228B22'
    elif rating >= 7: return '#32CD32'
    elif rating >= 6: return '#FFA500'
    elif rating >= 5: return '#FF6347'
    else: return '#DC143C'


def create_map(df: pd.DataFrame) -> folium.Map:
    """Create Folium map with listings."""
    valid_coords = df.dropna(subset=['lat', 'lon'])
    if valid_coords.empty:
        center = (42.36, -71.06)
    else:
        center = (valid_coords['lat'].mean(), valid_coords['lon'].mean())

    m = folium.Map(location=center, zoom_start=11, tiles='OpenStreetMap')

    for _, row in df.iterrows():
        if pd.isna(row['lat']) or pd.isna(row['lon']):
            continue

        price_k = round(row['price'] / 1000, 1) if row['price'] else '?'
        color = get_color(row['elem'])

        if row['flags']:
            border_style = "2px dashed #666"
            opacity = 0.5
        else:
            border_style = "2px solid white"
            opacity = 0.9

        popup_html = f'''
        <b>{row["address"]}, {row["city"]}</b><br>
        <b>${row["price"]:,}</b> | {row["beds"]}bd/{row["baths"]}ba | {row["sqft"] or "?"} sqft<br>
        <b>Schools:</b> Elem {row["elem"]}, Mid {row["mid"]}, High {row["high"]}<br>
        {f'Assigned elem: <b>{row["elem_school"]}</b><br>' if row.get("elem_school") else ''}
        {f'<span style="color:#b26a00;">{row["elem_confirm"]}</span><br>' if row.get("elem_confirm") else ''}
        District: {row["district"]}{f' ({row["district_grades"]})' if row.get("district_grades") else ''}<br>
        {f'HS District: {row["district_hs"]} ({row["district_hs_grades"]})<br>' if row.get("district_hs") else ''}
        {f'<b style="color:red;">Flags: {row["flags"]}</b><br>' if row["flags"] else ''}
        <a href="{row["url"]}" target="_blank">View Listing</a>
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
            font-size: 11px;
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
            tooltip=f"{row['address']} | {row['beds']}bd | Elem:{row['elem']}"
        ).add_to(m)

    return m


# Sidebar
st.sidebar.title("🏠 Rental Search")
st.sidebar.markdown("*School-aware rental finder*")

# Location input — search-as-you-type autocomplete over ~32k real US places,
# so the chosen location is always a valid "City, ST". Falls back to a plain
# text box if the streamlit-searchbox component isn't installed.
if _HAS_SEARCHBOX and load_cities():
    with st.sidebar:
        picked = st_searchbox(
            search_cities,
            placeholder="Start typing a city… (e.g., Northborough, MA)",
            default="Providence, RI",
            key="city_searchbox",
        )
    location = picked or st.session_state.get("last_location") or "Providence, RI"
else:
    location = st.sidebar.text_input("Location", value="Providence, RI",
                                     help="City, ST format (e.g., 'Austin, TX')")
radius = st.sidebar.slider("Search radius (miles)", 0, 50, 0, step=5,
                            help="0 = city only. >0 expands outward to nearby towns "
                                 "(nearest first) only until the target hits are filled "
                                 "or this radius is reached.")
limit = st.sidebar.slider("Target hits", 20, 200, 50, step=10,
                          help="Number of listings that pass the filters below. The search keeps scanning until it finds this many or runs out of listings.")

# Filters are set BEFORE searching — they drive the scan, which keeps going
# until it collects `limit` listings that pass (or exhausts the pool).
st.sidebar.subheader("Filters")
max_price = st.sidebar.number_input("Max price ($/month, 0 = no cap)",
                                    min_value=0, value=0, step=100)
min_beds = st.sidebar.selectbox("Min Bedrooms", [0, 1, 2, 3, 4, 5], index=0)
min_sqft = st.sidebar.number_input("Min sqft (0 = no minimum)",
                                   min_value=0, value=0, step=100,
                                   help="Listings without a square footage are "
                                        "excluded when this is set.")

_LEVEL_LABELS = {"Elementary": "elementary", "Intermediate": "middle", "High": "high"}
school_level_label = st.sidebar.radio(
    "School level to filter on", list(_LEVEL_LABELS), horizontal=True,
    help="The rating filter applies to this level only. All three are still "
         "shown in the results.")
school_level = _LEVEL_LABELS[school_level_label]
min_rating = st.sidebar.slider(f"Min {school_level_label} Rating",
                               0.0, 10.0, 0.0, step=0.5,
                               help="Where the assigned school is unknown, the "
                                    "rating shown is the WORST school in the "
                                    "district, so this filter is a floor rather "
                                    "than an estimate.")
hide_flagged = st.sidebar.checkbox("Hide flagged listings", value=False)
hide_units = st.sidebar.checkbox("Hide UNIT (apartments)", value=False)

radius_miles = radius if radius > 0 else None

# Search button
search_clicked = st.sidebar.button("🔍 Search", type="primary")

if st.sidebar.button("🔄 Clear Cache"):
    st.cache_data.clear()
    st.rerun()

# --- Saved-search notifications (ntfy) ---
# "Create notification" saves the CURRENT sidebar settings as a saved search;
# the daily notify.py job runs it and pushes new matches to the ntfy topic.
st.sidebar.markdown("---")
st.sidebar.subheader("🔔 Daily notification")
_notif_name = location + (f" +{radius}mi" if radius else "")
if min_rating > 0:
    _notif_name += f", {school_level} {min_rating:g}+"
notif_topic = st.sidebar.text_input(
    "ntfy topic", value=slugify(location),
    help="New matches get pushed to this ntfy topic daily. Subscribe to it in the ntfy app.")
if st.sidebar.button("🔔 Create notification"):
    if not location:
        st.sidebar.error("Pick a location first.")
    else:
        cfg = {
            "name": _notif_name,
            "location": location,
            "radius_miles": radius_miles,
            "limit": limit,
            "min_beds": min_beds if min_beds > 0 else None,
            "max_price": max_price if max_price > 0 else None,
            "min_rating": min_rating if min_rating > 0 else None,
            "school_level": school_level,
            "min_sqft": min_sqft or None,
            "hide_flagged": hide_flagged,
            "hide_units": hide_units,
            "topic": notif_topic or slugify(location),
        }
        try:
            saved = add_saved_search(cfg)
            st.sidebar.success(
                f"Saved “{_notif_name}” → topic ‘{cfg['topic']}’. "
                f"The daily job will push new matches ({len(saved)} saved search(es)).")
        except Exception as e:
            st.sidebar.error(f"Couldn't save notification: {e}")

# Initialize session state
if 'df' not in st.session_state:
    st.session_state.df = pd.DataFrame()
if 'last_location' not in st.session_state:
    st.session_state.last_location = None

# Run search
if search_clicked and location:
    st.session_state.df = fetch_listings(
        location, limit, radius_miles,
        min_beds if min_beds > 0 else None,
        max_price if max_price > 0 else None,
        min_rating if min_rating > 0 else None,
        school_level,
        min_sqft or None,
        hide_flagged, hide_units,
    )
    st.session_state.last_location = location

df = st.session_state.df

if df.empty:
    st.title("🏠 School-Aware Rental Search")
    if st.session_state.last_location:
        scanned = df.attrs.get('scanned', 0)
        pool = df.attrs.get('pool', 0)
        if pool:
            st.warning(f"No listings matched your filters — scanned {scanned} of {pool} listings, all discarded. Try loosening the filters.")
        else:
            st.warning(f"No listings found for {st.session_state.last_location}.")
    st.markdown("""
    Set your filters in the sidebar and click **Search** to find rentals with school ratings.
    The search keeps scanning until it finds your target number of matching listings.

    **Features:**
    - 📍 Interactive map with color-coded markers by school rating
    - 📊 Filterable data table
    - ⚠️ Warning flags for potentially misleading listings
    - 📥 CSV download

    **Map Legend:**
    - 🟢 Green = 7+ school rating (good)
    - 🟡 Orange = 6-7 rating (average)
    - 🔴 Red = <6 rating (below average)
    - Dashed border = has warning flags
    """)
    st.stop()

# City filter — cities are only known after results return, so this stays a
# post-search display narrowing.
st.sidebar.markdown("---")
cities = sorted(df['city'].dropna().unique())
if cities:
    selected_cities = st.sidebar.multiselect("Cities", cities, default=cities)
else:
    selected_cities = []

filtered = df.copy()
if selected_cities:
    filtered = filtered[filtered['city'].isin(selected_cities)]

# Main content
st.title(f"🏠 Rentals: {st.session_state.last_location}")

scanned = df.attrs.get('scanned', 0)
pool = df.attrs.get('pool', 0)
matched = df.attrs.get('matched', len(df))
target = df.attrs.get('limit', limit)
if matched < target:
    st.info(f"Found {matched} of {target} requested (scanned {scanned} of {pool} listings); the pool was exhausted.")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Hits", matched)
col2.metric("Scanned", scanned)
col3.metric("Filtered out", max(0, scanned - matched),
            help="Listings scanned but kept out of the target hits by your filters.")
col4.metric("Avg Elem Rating", f"{filtered['elem'].mean():.1f}" if filtered['elem'].notna().any() else "N/A")

# Map
st.subheader("Map")
if not filtered.empty:
    m = create_map(filtered)
    import hashlib
    map_key = hashlib.md5(filtered[['lat','lon','price','elem']].to_json().encode()).hexdigest()[:10]
    st_folium(m, width=None, height=500, key=f"map_{map_key}")
else:
    st.warning("No listings match your filters.")

# Legend
st.markdown("""
**Legend:** Circle color = Elementary school rating (🟢 8+ | 🟡 6-7 | 🔴 <5) | Number = Rent in $K | Dashed = Has flags
""")

# Table
st.subheader(f"Listings ({len(filtered)})")
if len(filtered) > 0:
    # Put the level being filtered on first, with its school and caveat, then
    # the other two levels for context.
    _pfx = {'elementary': 'elem', 'middle': 'mid', 'high': 'high'}[school_level]
    _others = [p for p in ('elem', 'mid', 'high') if p != _pfx]
    cols = (['address', 'city', 'price', 'beds', 'baths', 'sqft',
             _pfx, f'{_pfx}_best', f'{_pfx}_school', f'{_pfx}_confirm']
            + _others + ['district', 'district_hs', 'flags', 'url'])
    # Tolerate frames cached before district_hs existed.
    display_df = filtered[[c for c in cols if c in filtered.columns]].copy()
    display_df = display_df.sort_values('elem', ascending=False, na_position='last')

    st.dataframe(
        display_df,
        column_config={
            "url": st.column_config.LinkColumn("Listing"),
            "price": st.column_config.NumberColumn("Price", format="$%d"),
            "elem": st.column_config.NumberColumn("Elem", format="%.1f"),
            "mid": st.column_config.NumberColumn("Mid", format="%.1f"),
            "high": st.column_config.NumberColumn("High", format="%.1f"),
            "district": st.column_config.TextColumn("District"),
            "district_hs": st.column_config.TextColumn("HS District"),
            f"{_pfx}_school": st.column_config.TextColumn(
                f"Assigned {school_level_label}",
                help="The elementary school this address is zoned for (NCES SABS "
                     "2015-16). Blank means this district didn't participate."),
            f"{_pfx}_best": st.column_config.NumberColumn(
                "Best case", format="%.1f",
                help="When the assigned school is unknown, the best-rated school in "
                     "the district. Elem shows the worst. Blank means Elem is exact."),
            f"{_pfx}_confirm": st.column_config.TextColumn(
                "Certainty",
                help="Blank = Elem is the assigned school's own rating. "
                     "'worst case' = assigned school unknown, so Elem is the LOWEST "
                     "rated school in the district, i.e. a floor you can filter "
                     "against safely. Nearest-school guessing is wrong 44% of the "
                     "time, so we bound rather than guess."),
        },
        hide_index=True,
        width='stretch',
    )

    # Download button
    csv = filtered.to_csv(index=False)
    st.download_button(
        "📥 Download CSV",
        csv,
        f"{location.lower().replace(',', '').replace(' ', '_')}_rentals.csv",
        "text/csv",
    )
