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
"""


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
