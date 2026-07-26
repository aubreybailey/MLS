#!/usr/bin/env python3
"""
Local SQLite store for the rental search.

Two jobs:

1. A namespaced, TTL'd cache for slow/flaky external lookups — GreatSchools
   ratings, Overpass town discovery, town->ZIP resolution. These all re-answer
   the same questions on every search and every nightly notify run, and the
   Overpass/Nominatim ones are the flaky path that produced 504s and hangs.

2. (Coming) real tables for school data we own — MCAS / proficiency scores
   joined to schools — which is why this is a database and not a flat file.

Everything here degrades to None/no-op on error: a broken cache must fall back
to a live fetch, never take down a search.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get(
    'SCHOOLS_DB',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'schools.db'),
)

# Per-namespace freshness. School ratings are published annually; town geography
# and ZIP assignments effectively never change.
TTL_DAYS = {
    # v2 stores every rated school per cell, not just the first five -- needed
    # to attach a rating to an address's SABS-assigned school.
    'ratings_v2': 90,
    'ratings': 90,
    'towns': 365,
    'town_zip': 365,
}
DEFAULT_TTL_DAYS = 90

_conn = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

-- School directory, keyed by the NCES school id. ncessch is what SABS
-- attendance zones carry, so a zone lookup lands directly on a row here.
CREATE TABLE IF NOT EXISTS schools (
    ncessch    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    leaid      TEXT,              -- district; equals the TIGER district GEOID
    state      TEXT,
    city       TEXT,
    lat        REAL,
    lon        REAL,
    grade_lo   INTEGER,
    grade_hi   INTEGER,
    level      TEXT,              -- elementary | middle | high | other
    enrollment INTEGER,
    source     TEXT DEFAULT 'nces',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS schools_leaid ON schools(leaid);
CREATE INDEX IF NOT EXISTS schools_geo   ON schools(lat, lon);
CREATE INDEX IF NOT EXISTS schools_state ON schools(state);

-- Ratings per school. Split from the directory because they refresh on a
-- different cadence and can come from different sources (scrape vs hand-entered).
CREATE TABLE IF NOT EXISTS school_ratings (
    ncessch    TEXT PRIMARY KEY,
    rating     REAL,
    matched_name TEXT,            -- the name the source used, for auditing matches
    source     TEXT DEFAULT 'greatschools',
    fetched_at TEXT,
    FOREIGN KEY (ncessch) REFERENCES schools(ncessch)
);

-- Attendance-zone assignment sampled per point from GreatSchools' "Schools by
-- Address" oracle (which carries current licensed zone data SABS lacks). Each
-- row is one lat/lon we asked about and the school it's assigned to. A listing
-- in an unzoned district resolves to its nearest sample -- a labeled point
-- cloud, which refines as more points are added, without fragile polygon
-- reconstruction. Keyed by rounded coords so re-sampling a point updates it.
CREATE TABLE IF NOT EXISTS zone_samples (
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    ncessch    TEXT,             -- assigned school, matched to NCES (NULL if unmatched)
    school_name TEXT,            -- as GreatSchools labeled it
    district   TEXT,            -- "Assigned school in <district>"
    state      TEXT,
    source     TEXT DEFAULT 'greatschools-assigned',
    fetched_at TEXT,
    PRIMARY KEY (lat, lon)
);
CREATE INDEX IF NOT EXISTS zone_samples_geo ON zone_samples(lat, lon);

-- Per-district metadata, keyed by the NCES leaid (= TIGER district GEOID).
-- zoning_style is how a district decides which school an address attends -- the
-- distinction that determines whether zone SAMPLING even works:
--   zoned   geographic attendance zones (Northborough, Concord, Natick) -> sample
--   choice  no geographic zones; lottery / district assignment (Boston, Acton-
--           Boxborough) -> DON'T sample, and label listings honestly, because a
--           sampled point would pin every address to one arbitrary school
--   single  one school at a level -> assignment is trivial
--   unknown not yet determined
-- Recording it stops us re-sampling a choice district and lets enrichment say
-- "district uses school choice" instead of implying a zone.
CREATE TABLE IF NOT EXISTS districts (
    leaid        TEXT PRIMARY KEY,
    name         TEXT,
    state        TEXT,
    zoning_style TEXT DEFAULT 'unknown',
    checked_at   TEXT,
    note         TEXT
);

-- What the city-table sweep verified, per city. The sweep can tell whether it
-- saw a city's whole roster (the page states a total), unlike the radius sweep
-- which cannot. Persisting it answers "where is our coverage actually complete?"
-- -- which is exactly what the attendance/rating gap analysis needs, instead of
-- that signal being printed once and lost.
CREATE TABLE IF NOT EXISTS city_coverage (
    state       TEXT NOT NULL,
    city        TEXT NOT NULL,
    total_listed INTEGER,         -- schools GreatSchools claims for this city
    seen        INTEGER,          -- rows we actually parsed
    matched     INTEGER,          -- rows linked to an NCES school
    complete    INTEGER,          -- 1 = we saw the full roster (seen >= total)
    swept_at    TEXT,
    PRIMARY KEY (state, city)
);
"""

# Ratings are published annually.
RATING_TTL_DAYS = 90


def _connect():
    """Open (once) a shared connection. WAL so the web UI and the notify cron
    can hit the same file concurrently without blocking each other."""
    global _conn
    if _conn is not None:
        return _conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # check_same_thread=False: enrichment runs in a ThreadPoolExecutor. Every
    # access below is serialized by _lock, so this stays safe.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    conn.commit()
    _conn = conn
    return _conn


def get(namespace: str, key: str, max_age_days: int = None):
    """Return a cached value, or None if absent/stale/unreadable."""
    if max_age_days is None:
        max_age_days = TTL_DAYS.get(namespace, DEFAULT_TTL_DAYS)
    try:
        with _lock:
            cur = _connect().execute(
                'SELECT fetched_at, payload FROM lookups WHERE namespace = ? AND key = ?',
                (namespace, key),
            )
            row = cur.fetchone()
        if row is None:
            return None
        if datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) > timedelta(days=max_age_days):
            return None
        return json.loads(row[1])
    except Exception:
        return None


def put(namespace: str, key: str, value) -> None:
    """Store/refresh a cached value. Never raises."""
    try:
        payload = json.dumps(value)
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn = _connect()
            conn.execute(
                'INSERT INTO lookups (namespace, key, fetched_at, payload) '
                'VALUES (?, ?, ?, ?) ON CONFLICT(namespace, key) DO UPDATE SET '
                'fetched_at = excluded.fetched_at, payload = excluded.payload',
                (namespace, key, now, payload),
            )
            conn.commit()
    except Exception:
        pass


def upsert_schools(rows: list) -> int:
    """Insert/refresh school directory rows. Each dict needs at least ncessch
    and name. Returns the number written."""
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    cols = ('ncessch', 'name', 'leaid', 'state', 'city', 'lat', 'lon',
            'grade_lo', 'grade_hi', 'level', 'enrollment', 'source')
    try:
        with _lock:
            conn = _connect()
            conn.executemany(
                f"INSERT INTO schools ({','.join(cols)}, updated_at) "
                f"VALUES ({','.join('?' * len(cols))}, ?) "
                f"ON CONFLICT(ncessch) DO UPDATE SET "
                + ', '.join(f'{c}=excluded.{c}' for c in cols[1:])
                + ", updated_at=excluded.updated_at",
                [tuple(r.get(c) for c in cols) + (now,) for r in rows],
            )
            conn.commit()
        return len(rows)
    except Exception:
        return 0


# Rating sources, most trusted first. A lower-priority write never overwrites a
# higher-priority one, so the CitySpire ~2020 seed can be layered underneath
# without ever clobbering a fresh scrape or a hand-entered value, regardless of
# the order scripts happen to run in.
SOURCE_PRIORITY = {'manual': 3, 'greatschools': 2, 'cityspire-2020': 1}


def _source_rank(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 2)   # unknown sources rank as a fresh scrape


def put_school_rating(ncessch: str, rating, matched_name: str = '',
                      source: str = 'greatschools') -> None:
    """Record a rating for one school, respecting source precedence. Never raises.

    A write is skipped when an equal-or-higher-priority rating already exists,
    so seeding can't overwrite fresh data and re-running a scrape can't be
    undone by a later seed pass."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn = _connect()
            cur = conn.execute(
                'SELECT source FROM school_ratings WHERE ncessch = ?', (ncessch,))
            row = cur.fetchone()
            if row is not None and _source_rank(row[0]) > _source_rank(source):
                return                       # keep the higher-priority rating
            conn.execute(
                'INSERT INTO school_ratings (ncessch, rating, matched_name, source, fetched_at) '
                'VALUES (?, ?, ?, ?, ?) ON CONFLICT(ncessch) DO UPDATE SET '
                'rating=excluded.rating, matched_name=excluded.matched_name, '
                'source=excluded.source, fetched_at=excluded.fetched_at',
                (ncessch, rating, matched_name, source, now),
            )
            conn.commit()
    except Exception:
        pass


def _rating_is_fresh(rating, fetched_at, source,
                     max_age_days: int = RATING_TTL_DAYS) -> bool:
    """Is a stored rating still usable? Hand-entered rows (source='manual')
    never expire -- you entered them deliberately."""
    if rating is None:
        return False
    if source != 'manual' and fetched_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
            if age > timedelta(days=max_age_days):
                return False
        except Exception:
            return False
    return True


def get_school_rating(ncessch: str, max_age_days: int = RATING_TTL_DAYS):
    """Rating for one school, or None if absent/stale."""
    try:
        with _lock:
            cur = _connect().execute(
                'SELECT rating, fetched_at, source FROM school_ratings WHERE ncessch = ?',
                (ncessch,),
            )
            row = cur.fetchone()
        if row is None or not _rating_is_fresh(row[0], row[1], row[2], max_age_days):
            return None
        return row[0]
    except Exception:
        return None


def get_school(ncessch: str):
    """One school directory row joined with its (fresh) rating, or None."""
    try:
        with _lock:
            cur = _connect().execute(
                'SELECT s.ncessch, s.name, s.leaid, s.state, s.city, s.lat, s.lon, '
                's.grade_lo, s.grade_hi, s.level, s.enrollment, '
                'r.rating, r.fetched_at, r.source '
                'FROM schools s LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
                'WHERE s.ncessch = ?', (ncessch,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ('ncessch', 'name', 'leaid', 'state', 'city', 'lat', 'lon',
                'grade_lo', 'grade_hi', 'level', 'enrollment')
        d = dict(zip(keys, row[:11]))
        rating, fetched_at, source = row[11], row[12], row[13]
        fresh = _rating_is_fresh(rating, fetched_at, source)
        d['rating'] = rating if fresh else None
        d['rating_source'] = source if fresh else None
        return d
    except Exception:
        return None


def schools_near(lat: float, lon: float, radius_miles: float = 5.0,
                 level: str = None, limit: int = 100) -> list:
    """Schools near a point, nearest first, each joined with its (fresh)
    rating as 'rating'/'rating_source'. Uses a bounding box in SQL (indexed)
    then exact haversine in Python -- fine at this table size."""
    import math
    try:
        dlat = radius_miles / 69.0
        dlon = radius_miles / max(0.1, 69.0 * math.cos(math.radians(lat)))
        sql = ('SELECT s.ncessch, s.name, s.leaid, s.state, s.city, s.lat, s.lon, '
               's.grade_lo, s.grade_hi, s.level, s.enrollment, '
               'r.rating, r.fetched_at, r.source '
               'FROM schools s LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
               'WHERE s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?')
        args = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
        if level:
            sql += ' AND s.level = ?'
            args.append(level)
        with _lock:
            rows = _connect().execute(sql, args).fetchall()

        def hav(la, lo):
            R = 3958.8
            x = (math.sin(math.radians(la - lat) / 2) ** 2
                 + math.cos(math.radians(lat)) * math.cos(math.radians(la))
                 * math.sin(math.radians(lo - lon) / 2) ** 2)
            return R * 2 * math.asin(math.sqrt(x))

        keys = ('ncessch', 'name', 'leaid', 'state', 'city', 'lat', 'lon',
                'grade_lo', 'grade_hi', 'level', 'enrollment')
        out = []
        for r in rows:
            d = dict(zip(keys, r[:11]))
            if d['lat'] is None or d['lon'] is None:
                continue
            d['distance_mi'] = hav(d['lat'], d['lon'])
            if d['distance_mi'] > radius_miles:
                continue
            rating, fetched_at, source = r[11], r[12], r[13]
            fresh = _rating_is_fresh(rating, fetched_at, source)
            d['rating'] = rating if fresh else None
            d['rating_source'] = source if fresh else None
            out.append(d)
        out.sort(key=lambda d: d['distance_mi'])
        return out[:limit]
    except Exception:
        return []


def schools_in_district(leaid: str, level: str = None) -> list:
    """Schools in one district with their ratings, for worst-case bounding.

    When we can't determine the assigned school, every school in the district
    is a candidate, so the minimum rating here is a floor the address cannot do
    worse than. Rows with rating=None are returned too -- the caller needs to
    know the bound is incomplete."""
    try:
        sql = ('SELECT s.ncessch, s.name, s.level, s.enrollment, r.rating '
               'FROM schools s LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
               'WHERE s.leaid = ?')
        args = [leaid]
        if level:
            sql += ' AND s.level = ?'
            args.append(level)
        with _lock:
            rows = _connect().execute(sql + ' ORDER BY s.name', args).fetchall()
        keys = ('ncessch', 'name', 'level', 'enrollment', 'rating')
        return [dict(zip(keys, r)) for r in rows]
    except Exception:
        return []


def put_zone_sample(lat: float, lon: float, ncessch, school_name: str,
                    district: str, state: str,
                    source: str = 'greatschools-assigned') -> None:
    """Store one sampled point->assigned-school. Never raises."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn = _connect()
            conn.execute(
                'INSERT INTO zone_samples (lat, lon, ncessch, school_name, '
                'district, state, source, fetched_at) VALUES (?,?,?,?,?,?,?,?) '
                'ON CONFLICT(lat, lon) DO UPDATE SET ncessch=excluded.ncessch, '
                'school_name=excluded.school_name, district=excluded.district, '
                'source=excluded.source, fetched_at=excluded.fetched_at',
                (round(lat, 5), round(lon, 5), ncessch, school_name, district,
                 (state or '').upper(), source, now),
            )
            conn.commit()
    except Exception:
        pass


def nearest_zone_sample(lat: float, lon: float, max_miles: float = 0.6):
    """Assigned school for the nearest sampled point within max_miles, or None.

    A bounded nearest-neighbour classifier over the labeled point cloud: close
    enough to a known point, an address is almost certainly in the same zone.
    Returns {'ncessch','school_name','district','distance_mi'} or None."""
    import math
    try:
        # Cheap bounding box first, then exact haversine on the survivors.
        dlat = max_miles / 69.0
        dlon = max_miles / max(0.1, 69.0 * math.cos(math.radians(lat)))
        with _lock:
            rows = _connect().execute(
                'SELECT lat, lon, ncessch, school_name, district FROM zone_samples '
                'WHERE ncessch IS NOT NULL AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
        best, best_d = None, max_miles
        for la, lo, nc, nm, dist in rows:
            R = 3958.8
            x = (math.sin(math.radians(la - lat) / 2) ** 2
                 + math.cos(math.radians(lat)) * math.cos(math.radians(la))
                 * math.sin(math.radians(lo - lon) / 2) ** 2)
            d = R * 2 * math.asin(math.sqrt(x))
            if d <= best_d:
                best, best_d = (nc, nm, dist), d
        if best is None:
            return None
        return {'ncessch': best[0], 'school_name': best[1],
                'district': best[2], 'distance_mi': round(best_d, 3)}
    except Exception:
        return None


VALID_ZONING = ('zoned', 'choice', 'single', 'unknown')


def set_district_zoning(leaid: str, zoning_style: str, name: str = None,
                        state: str = None, note: str = None) -> None:
    """Record how a district assigns schools. Never raises. name/state/note only
    overwrite when provided, so a later zoning update doesn't blank them."""
    if zoning_style not in VALID_ZONING:
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn = _connect()
            conn.execute(
                'INSERT INTO districts (leaid, name, state, zoning_style, checked_at, note) '
                'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(leaid) DO UPDATE SET '
                'zoning_style=excluded.zoning_style, checked_at=excluded.checked_at, '
                'name=COALESCE(excluded.name, districts.name), '
                'state=COALESCE(excluded.state, districts.state), '
                'note=COALESCE(excluded.note, districts.note)',
                (leaid, name, (state or '').upper() or None, zoning_style, now, note),
            )
            conn.commit()
    except Exception:
        pass


def get_district_zoning(leaid: str) -> str:
    """Return a district's zoning_style, or 'unknown' if not recorded."""
    try:
        with _lock:
            row = _connect().execute(
                'SELECT zoning_style FROM districts WHERE leaid = ?', (leaid,)).fetchone()
        return row[0] if row else 'unknown'
    except Exception:
        return 'unknown'


def record_city_coverage(state: str, city: str, total_listed, seen: int,
                         matched: int, complete: bool) -> None:
    """Persist one city's sweep result. Never raises."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn = _connect()
            conn.execute(
                'INSERT INTO city_coverage (state, city, total_listed, seen, '
                'matched, complete, swept_at) VALUES (?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(state, city) DO UPDATE SET '
                'total_listed=excluded.total_listed, seen=excluded.seen, '
                'matched=excluded.matched, complete=excluded.complete, '
                'swept_at=excluded.swept_at',
                (state.upper(), city, total_listed, seen, matched,
                 1 if complete else 0, now),
            )
            conn.commit()
    except Exception:
        pass


def city_coverage(state: str = None, complete_only: bool = False) -> list:
    """Read city_coverage rows, optionally filtered. Never raises."""
    try:
        sql = ('SELECT state, city, total_listed, seen, matched, complete, '
               'swept_at FROM city_coverage')
        clauses, args = [], []
        if state:
            clauses.append('state = ?'); args.append(state.upper())
        if complete_only:
            clauses.append('complete = 1')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY state, city'
        keys = ('state', 'city', 'total_listed', 'seen', 'matched', 'complete', 'swept_at')
        with _lock:
            return [dict(zip(keys, r)) for r in _connect().execute(sql, args)]
    except Exception:
        return []


def stats() -> dict:
    """Per-namespace counts and age range, for debugging."""
    try:
        with _lock:
            cur = _connect().execute(
                'SELECT namespace, COUNT(*), MIN(fetched_at), MAX(fetched_at) '
                'FROM lookups GROUP BY namespace ORDER BY namespace'
            )
            rows = cur.fetchall()
        return {
            'path': DB_PATH,
            'namespaces': [
                {'name': n, 'entries': c, 'oldest': o, 'newest': w} for n, c, o, w in rows
            ],
        }
    except Exception as e:
        return {'path': DB_PATH, 'namespaces': [], 'error': str(e)}


if __name__ == '__main__':
    s = stats()
    print(f"local db: {s['path']}")
    if s.get('error'):
        print(f"  error: {s['error']}")
    elif not s['namespaces']:
        print("  (empty)")
    else:
        for ns in s['namespaces']:
            print(f"  {ns['name']:<10} {ns['entries']:>6} entries   "
                  f"oldest {ns['oldest'][:10]}  newest {ns['newest'][:10]}")
