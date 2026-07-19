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


def put_school_rating(ncessch: str, rating, matched_name: str = '',
                      source: str = 'greatschools') -> None:
    """Record a rating for one school. Never raises."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            conn = _connect()
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


def get_school_rating(ncessch: str, max_age_days: int = RATING_TTL_DAYS):
    """Rating for one school, or None if absent/stale. Hand-entered rows
    (source='manual') never expire -- you entered them deliberately."""
    try:
        with _lock:
            cur = _connect().execute(
                'SELECT rating, fetched_at, source FROM school_ratings WHERE ncessch = ?',
                (ncessch,),
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        if row[2] != 'manual' and row[1]:
            if datetime.now(timezone.utc) - datetime.fromisoformat(row[1]) > timedelta(days=max_age_days):
                return None
        return row[0]
    except Exception:
        return None


def schools_near(lat: float, lon: float, radius_miles: float = 5.0,
                 level: str = None, limit: int = 100) -> list:
    """Schools near a point, nearest first. Uses a bounding box in SQL (indexed)
    then exact haversine in Python -- fine at this table size."""
    import math
    try:
        dlat = radius_miles / 69.0
        dlon = radius_miles / max(0.1, 69.0 * math.cos(math.radians(lat)))
        sql = ('SELECT ncessch, name, leaid, state, city, lat, lon, grade_lo, '
               'grade_hi, level, enrollment FROM schools '
               'WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?')
        args = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
        if level:
            sql += ' AND level = ?'
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
            d = dict(zip(keys, r))
            if d['lat'] is None or d['lon'] is None:
                continue
            d['distance_mi'] = hav(d['lat'], d['lon'])
            if d['distance_mi'] <= radius_miles:
                out.append(d)
        out.sort(key=lambda d: d['distance_mi'])
        return out[:limit]
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
