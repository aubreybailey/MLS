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

# Preferred backend: one indexed GeoPackage instead of 358 shapefiles.
# Build it with scripts/build_geopackage.py; we fall back to shapefiles if absent.
GPKG_PATH = os.path.join(DATA_DIR, 'school_districts.gpkg')
DISTRICT_LEVELS = [("unsd", "unified"), ("elsd", "elementary"), ("scsd", "secondary")]

# NCES SABS attendance zones (scripts/build_attendance_zones.py). Optional:
# coverage is ~60% of MA districts, so absence is normal and must be reported
# as unknown, never approximated. See ZONES_UNAVAILABLE_NOTE.
ZONES_PATH = os.path.join(DATA_DIR, 'attendance_zones.gpkg')

# Why we refuse to fall back to nearest-school: measured against real SABS
# zones across 3048 sampled points in MA multi-school districts, picking the
# nearest school gives the wrong school 43.6% of the time.
NEAREST_SCHOOL_ERROR_RATE = 0.436

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

    for dtype, label in DISTRICT_LEVELS:
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


@lru_cache(maxsize=1)
def _zone_layers() -> tuple:
    """State layers present in the attendance-zone GeoPackage, if built."""
    if not os.path.exists(ZONES_PATH):
        return ()
    try:
        import pyogrio
        return tuple(l[0] for l in pyogrio.list_layers(ZONES_PATH))
    except Exception:
        return ()


def lookup_attendance_zone(lat: float, lon: float, state: str | None = None) -> dict:
    """Which school's attendance zone contains this point?

    Returns {'status': 'assigned'|'unzoned'|'unavailable', ...}. We never guess:
    'unzoned' means this district did not participate in SABS, and the caller
    should surface that as "confirm this yourself" rather than substituting a
    nearest-school or area-average figure.
    """
    layers = _zone_layers()
    if not layers:
        return {'status': 'unavailable', 'reason': 'attendance zone data not built'}

    search = [state.upper()] if state and state.upper() in layers else list(layers)
    point = Point(lon, lat)
    for layer in search:
        try:
            cands = gpd.read_file(ZONES_PATH, layer=layer, bbox=(lon, lat, lon, lat))
        except Exception:
            continue
        if cands.empty:
            continue
        hits = cands[cands.geometry.contains(point)]
        if hits.empty:
            continue
        # A point can sit in several zones (elementary + middle + high). Return
        # them keyed by level so callers can pick the one they care about.
        out = {}
        for _, r in hits.iterrows():
            out[str(r.get('level') or '?')] = {
                'school': r.get('schnam', ''),
                'ncessch': r.get('ncessch', ''),
                'leaid': r.get('leaid', ''),
                'grades': f"{r.get('gslo', '?')}-{r.get('gshi', '?')}",
                'open_enroll': r.get('openEnroll'),
            }
        return {'status': 'assigned', 'zones': out}

    return {'status': 'unzoned',
            'reason': 'district did not participate in the SABS collection'}


def _lookup_gpkg(lat: float, lon: float) -> dict | None:
    """Point-in-polygon against the national GeoPackage layers.

    The bbox filter is pushed down to GDAL, which answers it from the
    GeoPackage's R-tree index -- so we read only the handful of candidate
    polygons covering this point, never a whole state (let alone all of them).
    """
    point = Point(lon, lat)
    results = {}
    state_fips = None

    for dtype, label in DISTRICT_LEVELS:
        try:
            candidates = gpd.read_file(
                GPKG_PATH, layer=dtype, bbox=(lon, lat, lon, lat)
            )
        except Exception:
            continue                      # layer absent (e.g. no scsd) or unreadable

        if candidates.empty:
            continue

        # bbox is an approximation (it matches polygon envelopes), so still do
        # the exact containment test.
        matches = candidates[candidates.geometry.contains(point)]
        if matches.empty:
            continue

        row = matches.iloc[0]
        results[label] = {
            "name": row["NAME"],
            "geoid": row["GEOID"],
            "low_grade": row.get("LOGRADE", ""),
            "high_grade": row.get("HIGRADE", ""),
        }
        state_fips = state_fips or row.get("STATE_FIPS")

    if not results:
        return None
    return {"school_districts": results, "state_fips": state_fips}


def lookup_coords(lat: float, lon: float) -> dict:
    """
    Direct lookup from coordinates.
    Uses the GeoPackage when built; otherwise scans per-state shapefiles.
    """
    result = {
        "coordinates": {"lat": lat, "lon": lon},
        "school_districts": None,
        "error": None
    }

    if os.path.exists(GPKG_PATH):
        found = _lookup_gpkg(lat, lon)
        if found:
            result.update(found)
            return result
        result["error"] = "No matching school district found in available data"
        return result

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
