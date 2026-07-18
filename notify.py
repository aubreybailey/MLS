#!/usr/bin/env python3
"""Saved-search runner + ntfy pusher.

Reads saved searches (created via the web UI "Create notification" button, or by
hand), runs each through search_and_enrich, and pushes only listings that are NEW
since the last run to an ntfy topic. Intended to be run daily (e.g. from cron).

Config/state live under ./notify/ by default (override with env vars):
  SAVED_SEARCHES  path to saved_searches.json   (default: notify/saved_searches.json)
  NOTIFY_STATE    path to dedup state json       (default: notify/notify_state.json)
  NTFY_SERVER     base URL of the ntfy server    (default: http://192.168.1.4)

Usage:
  python notify.py                 # run all saved searches, push new listings
  python notify.py --dry-run       # run + print what WOULD be pushed, send nothing
  python notify.py --list          # show configured saved searches
"""
import os
import re
import sys
import json
import argparse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from search import search_and_enrich

_HERE = os.path.dirname(os.path.abspath(__file__))
SAVED_SEARCHES_PATH = os.environ.get("SAVED_SEARCHES",
                                     os.path.join(_HERE, "notify", "saved_searches.json"))
NOTIFY_STATE_PATH = os.environ.get("NOTIFY_STATE",
                                   os.path.join(_HERE, "notify", "notify_state.json"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "http://192.168.1.4")

# Fields of a saved search that get passed straight to search_and_enrich.
SEARCH_FIELDS = ("location", "radius_miles", "limit", "min_beds", "max_price",
                 "min_elem", "hide_flagged", "hide_units")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def slugify(text: str) -> str:
    """ntfy-safe topic fragment: lowercase, alnum + dashes."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def load_searches() -> list:
    data = _load_json(SAVED_SEARCHES_PATH, [])
    return data.get("searches", []) if isinstance(data, dict) else data


def add_saved_search(cfg: dict) -> list:
    """Append (or replace, by name) a saved search and persist. Returns the list."""
    searches = load_searches()
    searches = [s for s in searches if s.get("name") != cfg.get("name")]
    searches.append(cfg)
    _save_json(SAVED_SEARCHES_PATH, searches)
    return searches


def listing_key(row: dict) -> str:
    """Stable identity for dedup: listing URL, else address+city."""
    return row.get("url") or f"{row.get('address', '')}|{row.get('city', '')}"


def ntfy_publish(topic: str, title: str, message: str,
                 priority: str = None, tags=None, click: str = None,
                 server: str = None) -> int:
    base = (server or NTFY_SERVER).rstrip("/")
    headers = {"Title": title}                      # keep header values ASCII
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{base}/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def _format_listing(r: dict) -> str:
    price = f"${int(r['price']):,}" if r.get("price") else "$?"
    beds = f"{r['beds']}bd" if r.get("beds") is not None else "?bd"
    elem = f"Elem {r['elem']}" if r.get("elem") is not None else "Elem ?"
    line = f"{price} {beds} {elem} - {r.get('address', '')}, {r.get('city', '')}"
    url = r.get("url")
    return f"{line}\n{url}" if url else line


def process_search(cfg: dict, state: dict, dry_run: bool = False,
                   verbose: bool = False, server: str = None) -> dict:
    """Run one saved search and push new listings. Mutates `state` in place; the
    baseline for a search is only committed after a successful send (so a failed
    push is retried next run). First encounter seeds silently."""
    name = cfg.get("name") or cfg["location"]
    topic = cfg.get("topic") or slugify(name)
    kwargs = {k: cfg[k] for k in SEARCH_FIELDS if k in cfg}
    df = search_and_enrich(verbose=verbose, **kwargs)

    rows = df.to_dict("records") if not df.empty else []
    current = [listing_key(r) for r in rows]
    key_to_row = dict(zip(current, rows))

    if name not in state:                           # first run: seed, don't notify
        if not dry_run:
            state[name] = current
        return {"name": name, "total": len(rows), "new": 0, "status": "seeded"}

    prev = set(state[name])
    new_rows = [key_to_row[k] for k in current if k not in prev]

    if not new_rows:
        if not dry_run:
            state[name] = current
        return {"name": name, "total": len(rows), "new": 0, "status": "no new"}

    body = "\n\n".join(_format_listing(r) for r in new_rows[:8])
    if len(new_rows) > 8:
        body += f"\n\n…and {len(new_rows) - 8} more"
    title = f"{len(new_rows)} new rental{'s' if len(new_rows) != 1 else ''}: {name}"

    if dry_run:
        return {"name": name, "total": len(rows), "new": len(new_rows),
                "status": "dry-run (not sent)", "topic": topic, "body": body}

    ntfy_publish(topic, title, body, priority=cfg.get("priority", "default"),
                 tags=cfg.get("tags", ["house"]), click=new_rows[0].get("url"),
                 server=server)
    state[name] = current                           # commit only after a good send
    return {"name": name, "total": len(rows), "new": len(new_rows),
            "status": f"sent -> {topic}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Run searches and print what would be pushed; send nothing, save no state.")
    ap.add_argument("--list", action="store_true", help="List saved searches and exit.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Show search progress.")
    args = ap.parse_args()

    searches = load_searches()
    if args.list:
        if not searches:
            print(f"No saved searches in {SAVED_SEARCHES_PATH}")
        for s in searches:
            print(f"- {s.get('name', s.get('location'))}  ->  topic '{s.get('topic') or slugify(s.get('name', s['location']))}'")
        return 0

    if not searches:
        print(f"No saved searches in {SAVED_SEARCHES_PATH}. Create one from the web UI "
              f"or copy notify/saved_searches.example.json.")
        return 1

    print(f"ntfy server: {NTFY_SERVER}  |  {len(searches)} saved search(es)"
          + ("  [DRY RUN]" if args.dry_run else ""))
    state = _load_json(NOTIFY_STATE_PATH, {})
    total_new = 0
    for cfg in searches:
        try:
            res = process_search(cfg, state, dry_run=args.dry_run, verbose=args.verbose)
            total_new += res["new"]
            print(f"  {res['name']}: {res['new']} new of {res['total']} matches ({res['status']})")
            if args.dry_run and res.get("body"):
                print("    --- would push ---")
                for line in res["body"].splitlines():
                    print(f"    {line}")
        except Exception as e:
            print(f"  {cfg.get('name', cfg.get('location', '?'))}: ERROR {e}", file=sys.stderr)

    if not args.dry_run:
        _save_json(NOTIFY_STATE_PATH, state)
    print(f"Done. {total_new} new listing(s) total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
