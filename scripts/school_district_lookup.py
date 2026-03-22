#!/usr/bin/env python3
"""
School District Lookup Tool

Takes coordinates and performs point-in-polygon query against
TIGER/Line school district boundaries.
"""

import os
import glob
import geopandas as gpd
from shapely.geometry import Point
from functools import lru_cache

# Data directory - relative to app root
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')

# State abbreviation to FIPS code mapping
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72"
}


@lru_cache(maxsize=60)
def load_state_districts(state_fips: str, district_type: str = "unsd") -> gpd.GeoDataFrame | None:
    """
    Load school district shapefile for a state.
    district_type: 'unsd' (unified), 'elsd' (elementary), 'scsd' (secondary)
    """
    pattern = os.path.join(DATA_DIR, f"tl_2023_{state_fips}_{district_type}.shp")
    files = glob.glob(pattern)

    if not files:
        return None

    gdf = gpd.read_file(files[0])
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


def find_school_district(lat: float, lon: float, state_fips: str) -> dict | None:
    """Find which school district a point falls within."""
    point = Point(lon, lat)
    results = {}

    for dtype, label in [("unsd", "unified"), ("elsd", "elementary"), ("scsd", "secondary")]:
        gdf = load_state_districts(state_fips, dtype)
        if gdf is None:
            continue

        mask = gdf.geometry.contains(point)
        matches = gdf[mask]

        if not matches.empty:
            row = matches.iloc[0]
            results[label] = {
                "name": row["NAME"],
                "geoid": row["GEOID"],
                "low_grade": row.get("LOGRADE", ""),
                "high_grade": row.get("HIGRADE", ""),
            }

    return results if results else None


def lookup_coords(lat: float, lon: float) -> dict:
    """
    Direct lookup from coordinates.
    Tries all available state shapefiles to find matching district.
    """
    result = {
        "coordinates": {"lat": lat, "lon": lon},
        "school_districts": None,
        "error": None
    }

    available_states = set()
    for f in glob.glob(os.path.join(DATA_DIR, "tl_2023_*_unsd.shp")):
        fips = os.path.basename(f).split("_")[2]
        available_states.add(fips)

    for state_fips in available_states:
        districts = find_school_district(lat, lon, state_fips)
        if districts:
            result["school_districts"] = districts
            result["state_fips"] = state_fips
            return result

    result["error"] = "No matching school district found in available data"
    return result
