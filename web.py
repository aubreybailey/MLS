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
def fetch_listings(location: str, limit: int = 100, radius_miles: float = None) -> pd.DataFrame:
    """Fetch and annotate listings via search_and_enrich."""
    progress = st.progress(0, text=f"Fetching listings from {location}...")
    try:
        df = search_and_enrich(location=location, limit=limit,
                               radius_miles=radius_miles, verbose=False)
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
        District: {row["district"]}<br>
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
radius = st.sidebar.slider("Search radius (miles)", 0, 25, 0, step=5,
                            help="0 = city only. >0 searches surrounding zip codes too.")
limit = st.sidebar.slider("Max listings", 20, 200, 50, step=10)

radius_miles = radius if radius > 0 else None

# Search button
search_clicked = st.sidebar.button("🔍 Search", type="primary")

if st.sidebar.button("🔄 Clear Cache"):
    st.cache_data.clear()
    st.rerun()

# Initialize session state
if 'df' not in st.session_state:
    st.session_state.df = pd.DataFrame()
if 'last_location' not in st.session_state:
    st.session_state.last_location = None

# Run search
if search_clicked and location:
    st.session_state.df = fetch_listings(location, limit, radius_miles)
    st.session_state.last_location = location

df = st.session_state.df

if df.empty:
    st.title("🏠 School-Aware Rental Search")
    st.markdown("""
    Enter a location in the sidebar and click **Search** to find rentals with school ratings.

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

# Filters
st.sidebar.markdown("---")
st.sidebar.subheader("Filters")

# Price filter
prices = df['price'].dropna()
if not prices.empty:
    price_min, price_max = int(prices.min()), int(prices.max())
    price_range = st.sidebar.slider(
        "Price Range ($)",
        min_value=price_min,
        max_value=price_max,
        value=(price_min, min(5000, price_max)),
        step=100
    )
else:
    price_range = (0, 10000)

# Beds filter
beds_min = st.sidebar.selectbox("Min Bedrooms", [0, 1, 2, 3, 4, 5], index=0)

# School rating filter
elem_min = st.sidebar.slider("Min Elementary Rating", 0.0, 10.0, 0.0, step=0.5)

# Flags filter
hide_flagged = st.sidebar.checkbox("Hide flagged listings", value=False)
hide_units = st.sidebar.checkbox("Hide UNIT (apartments)", value=False)

# City filter
cities = sorted(df['city'].dropna().unique())
if cities:
    selected_cities = st.sidebar.multiselect("Cities", cities, default=cities)
else:
    selected_cities = []

# Apply filters
filtered = df.copy()
filtered = filtered[
    (filtered['price'].fillna(0) >= price_range[0]) &
    (filtered['price'].fillna(999999) <= price_range[1])
]
filtered = filtered[filtered['beds'].fillna(0) >= beds_min]
filtered = filtered[filtered['elem'].fillna(0) >= elem_min]
if selected_cities:
    filtered = filtered[filtered['city'].isin(selected_cities)]
if hide_flagged:
    filtered = filtered[filtered['flags'] == '']
if hide_units:
    filtered = filtered[~filtered['flags'].str.contains('UNIT', na=False)]

# Main content
st.title(f"🏠 Rentals: {st.session_state.last_location}")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Found", len(df))
col2.metric("After Filters", len(filtered))
col3.metric("Clean (no flags)", len(filtered[filtered['flags'] == '']))
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
    display_df = filtered[['address', 'city', 'price', 'beds', 'baths', 'sqft',
                           'elem', 'mid', 'high', 'district', 'flags', 'url']].copy()
    display_df = display_df.sort_values('elem', ascending=False, na_position='last')

    st.dataframe(
        display_df,
        column_config={
            "url": st.column_config.LinkColumn("Listing"),
            "price": st.column_config.NumberColumn("Price", format="$%d"),
            "elem": st.column_config.NumberColumn("Elem", format="%.1f"),
            "mid": st.column_config.NumberColumn("Mid", format="%.1f"),
            "high": st.column_config.NumberColumn("High", format="%.1f"),
        },
        hide_index=True,
        use_container_width=True,
    )

    # Download button
    csv = filtered.to_csv(index=False)
    st.download_button(
        "📥 Download CSV",
        csv,
        f"{location.lower().replace(',', '').replace(' ', '_')}_rentals.csv",
        "text/csv",
    )
