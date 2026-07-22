# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A school-aware rental search app (Streamlit web UI + CLI). Its whole reason for
existing is that mainstream rental sites show a school rating that is some
average of whatever schools are nearby, which is close to useless because most
districts have several schools of differing quality and a radius average even
crosses district lines. This app resolves the **specific school an address is
zoned into** where that is knowable, and reports a **guaranteed worst-case
floor** where it isn't — never an optimistic guess, never null. Read the README
"Why this exists" and "How the school rating works" sections before touching
rating logic.

## Commands

```bash
# Deploy / run (podman-compose under the hood)
docker compose build web cli          # REQUIRED after any code change — code is COPY'd into images
docker compose up -d web              # web UI at http://localhost:8501
docker compose run --rm cli "Providence, RI" --limit 25 --school-level elementary --min-rating 7

# One-off CLI enrichment during dev (mounts live code, no rebuild):
docker run --rm -e PROJ_DATA=/opt/conda/envs/rental-search/share/proj \
  -e SCHOOLS_DB=/app/cache/schools.db --network host \
  -v "$(pwd)/cache:/app/cache" -v "$(pwd)/data:/app/data:ro" -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/search.py:/app/search.py:ro" -v "$(pwd)/api.py:/app/api.py:ro" \
  -v "$(pwd)/scripts:/app/scripts:ro" -v "$(pwd)/db.py:/app/db.py:ro" \
  -w /app/output --entrypoint python mls-cli /app/search.py "Northborough, MA" --limit 3 --no-map

# Build the school data (once per state; idempotent + resumable):
python scripts/setup_state.py --state MA           # runs all 5 steps in order
python scripts/setup_state.py --state MA --only ratings

# Tests (plain scripts, no pytest):
docker run --rm -v "$(pwd)/scripts:/app/scripts:ro" --entrypoint python mls-cli /app/scripts/test_school_match.py
docker run --rm -e SCHOOLS_DB=/app/cache/schools.db -v "$(pwd)/cache:/app/cache" \
  -v "$(pwd)/scripts:/app/scripts:ro" -v "$(pwd)/api.py:/app/api.py:ro" -v "$(pwd)/db.py:/app/db.py:ro" \
  --entrypoint python mls-cli /app/scripts/test_api.py
```

## Deploy gotchas (these have all bitten before)

- `docker compose build web`/`build cli` is **mandatory after every code change** —
  `web` and `cli` are separate images and both COPY the source in.
- `podman restart <container>` reuses the **old image layer** and will NOT pick up
  a rebuild. To deploy new code you must recreate: `docker compose up -d --force-recreate web`
  (or `podman rm -f mls-web-1 && docker compose up -d web`).
- The container's HEALTHCHECK calls `curl`, which isn't in the conda image, so it
  **always reports "unhealthy" even when the app is fine**. Verify with a real
  request instead: `python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').read())"`.
  Host `curl` to the port is also intermittently flaky under rootless podman/pasta.
- `docker compose down` may error with a netns permission message but still work;
  trust `docker ps` + a real request, not `down`'s exit code.
- Git remote is HTTPS (no SSH key in this env); `git push` works via `gh` credentials.

## Architecture

### The `api.py` facade — the load-bearing design decision

**Every read the app performs goes through `api.py`.** It is the only module that
touches the database, the GeoPackage lookups, GreatSchools, Nominatim/Overpass,
and homeharvest. Everything is a plain function with scalar/dict args and
JSON-serializable returns — **no pandas or streamlit types in any signature** (pandas
is used internally only to preserve NaN semantics). This is deliberate so an MCP
server can wrap `get_schools`/`resolve_school`/`get_districts`/`enrich_listing`
directly as tools. Preserve this constraint.

The layering around it:
- `api.py` — all data access + the school-rating resolution logic.
- `search.py` — quota-fill orchestration, DataFrame assembly, CLI, map generation.
- `web.py` / `notify.py` — thin filtering/joining over what `search.py`/`api.py` return.

`enrich_listing()` returns a **~30-key contract dict**. Every CSV column, web table
column, and notify field reads from it, so changing/removing a key silently breaks
all three consumers — `scripts/test_api.py` guards this contract and is the most
important test.

### The rating model (core domain logic, in `api.py::_resolve_level`)

For each school level (elementary/middle/high), resolve in this precedence and
record the `*_source`:

| source | rating is | certainty |
|---|---|---|
| `zoned` | the address's assigned school (from NCES SABS attendance zones) | exact |
| `district-sole` | the district's only school at that level | exact |
| `district-min` | the **worst-rated** school in the district (`*_best` holds the max) | a floor |
| `zoned-unrated` | area average; school known but GreatSchools has no rating | unknown |
| `area-avg` | ~3mi radius average | **not a bound** — last resort |

**Invariant: the rating shown/filtered is never optimistic and never null.** When the
assigned school is unknown, `district-min` returns the district's *worst* school so
that `--min-rating 7` means "no school this address could be assigned to rates below
7". Filtering (`search.py::_passes`) reads `elem`/`mid`/`high` (= the worst/floor);
`*_best` is display-only and must never drive a filter or sort. "Nearest school" is
deliberately never used as a fallback — measured wrong 43.6% of the time.

Two facts wired into the resolution: TIGER district `GEOID` **equals** the NCES
`leaid` (that's the join key across boundaries, zones, and the schools table); and
`high` school falls back to the *secondary* district's leaid when one exists, since
it's usually a different district from elementary.

### Storage (`db.py`, at `cache/schools.db`)

One SQLite file, three roles: a namespaced TTL cache (`lookups` table:
GreatSchools area ratings, Overpass towns, town→ZIP — everything degrades to a live
fetch on error, never raises); a `schools` directory keyed by `ncessch`; and
`school_ratings` (per-school, split out because it refreshes on a different cadence
and can be hand-entered with `source='manual'`, which never expires).

### Data pipeline (`scripts/setup_state.py`, per state)

`boundaries` (Census TIGER shapefiles) → `geopackage` (merge into one indexed
`school_districts.gpkg`) → `zones` (NCES SABS attendance boundaries →
`attendance_zones.gpkg`) → `schools` (NCES CCD directory → db) → `ratings`
(GreatSchools, matched back to `ncessch`). Each step is idempotent and resumable.

Three upstream sources fail in ways that produce **silently incomplete data**, and
the scripts work around each — do not "simplify" these away:
- census.gov rate-limits with HTTP 429 (retry with backoff; a naive run reads it as
  "state has no such district type" and, under `set -e`, aborts the whole download).
- GreatSchools caps results at **25 per page regardless of radius** (must paginate;
  widening the radius makes coverage worse, not better).
- GreatSchools answers an empty search with HTTP **404**, not an error (rural radius
  escalation handles it).

### School name matching (`scripts/school_match.py`)

GreatSchools names ↔ NCES names is deterministic and **intentionally not fuzzy/ML**:
measured, character similarity cannot separate true matches from wrong ones (the
highest-scoring pair in a sample was a *wrong* one). Tiers: token-set containment →
exact-normalized → grade-span tiebreak → locally-distinctive-token. Ambiguity is
refused (returns `None`), because a wrong rating on the wrong school is worse than a
missing one. NOISE (dropped tokens) is deliberately narrow — level words like
`elementary`/`middle`/`high` are kept because MA names schools town+level. Any change
here must keep `scripts/test_school_match.py` green (its MUST_NOT_MATCH cases matter
most).

## Data & git

`data/`, `cache/`, `output/`, and the built GeoPackages are all gitignored and fully
regenerable from the scripts — never commit them (an earlier LFS mistake bloated the
repo to 500MB+). `us_cities.csv` (the autocomplete list) IS committed and is
regenerated by `scripts/build_cities.py`; it merges the Census Places gazetteer with
New England county subdivisions, since New England towns are MCDs absent from Places.
