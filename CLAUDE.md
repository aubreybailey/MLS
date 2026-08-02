# CLAUDE.md

## What this is

A school-aware rental search app (Streamlit web UI + CLI). Resolves the
**specific school an address is zoned into** where knowable, and reports a
**guaranteed worst-case floor** where it isn't — never an optimistic guess,
never null.

Default build uses only public government data. `--non-free` adds scraped
GreatSchools ratings. Licensed CC BY-NC-SA 4.0.

## Commands

```bash
# Deploy / run (podman-compose under the hood)
docker compose build web cli          # REQUIRED after any code change
docker compose up -d web              # web UI at http://localhost:8501
docker compose run --rm cli "Providence, RI" --limit 25 --school-level elementary --min-rating 7

# Dev: mount live code, no rebuild
docker run --rm -e PROJ_DATA=/opt/conda/envs/rental-search/share/proj \
  -e SCHOOLS_DB=/app/cache/schools.db --network host \
  -v "$(pwd)/cache:/app/cache" -v "$(pwd)/data:/app/data:ro" -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/search.py:/app/search.py:ro" -v "$(pwd)/api.py:/app/api.py:ro" \
  -v "$(pwd)/scripts:/app/scripts:ro" -v "$(pwd)/db.py:/app/db.py:ro" \
  -w /app/output --entrypoint python mls-cli /app/search.py "Northborough, MA" --limit 3 --no-map

# Build school data (once per state; idempotent + resumable)
python scripts/setup_state.py --state MA              # free (government only)
python scripts/setup_state.py --state MA --non-free    # + GreatSchools
python scripts/setup_state.py --state MA --only mcas   # re-run one step

# Tests (plain scripts, no pytest)
docker run --rm -v "$(pwd)/scripts:/app/scripts:ro" --entrypoint python mls-cli /app/scripts/test_school_match.py
docker run --rm -e SCHOOLS_DB=/app/cache/schools.db -v "$(pwd)/cache:/app/cache" \
  -v "$(pwd)/scripts:/app/scripts:ro" -v "$(pwd)/api.py:/app/api.py:ro" -v "$(pwd)/db.py:/app/db.py:ro" \
  --entrypoint python mls-cli /app/scripts/test_api.py
```

## Deploy gotchas (these have all bitten before)

- `docker compose build web`/`build cli` is **mandatory after every code change** —
  `web` and `cli` are separate images and both COPY the source in.
- `podman restart <container>` reuses the **old image layer** and will NOT pick up
  a rebuild. Must recreate: `docker compose up -d --force-recreate web`
- The HEALTHCHECK always reports "unhealthy" (curl not in image). Verify with:
  `python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').read())"`
- `docker compose down` may error with netns permission message but still work.
  Trust `docker ps` + a real request.
- Git remote is HTTPS (no SSH key); `git push` works via `gh` credentials.

## Architecture

### The `api.py` facade

**Every read the app performs goes through `api.py`.** Plain functions with
scalar/dict args and JSON-serializable returns — no pandas or streamlit types
in signatures. This is deliberate for MCP wrappability. Preserve this.

Layering:
- `api.py` — all data access + school-rating resolution logic
- `search.py` — quota-fill orchestration, DataFrame assembly, CLI, maps
- `web.py` / `notify.py` — thin filtering/joining

`enrich_listing()` returns a **~30-key contract dict**. Every CSV column, web
table column, and notify field reads from it. `scripts/test_api.py` guards
this contract.

### If you're modifying rating logic

Read `docs/rating-resolution.md` first. Key invariants:
- The rating is never optimistic and never null
- `*_best` is display-only — must never drive a filter or sort
- "Nearest school" is deliberately never used (wrong 43.6% of the time)
- TIGER `GEOID` = NCES `leaid` (the join key everywhere)
- `high` level falls back to the secondary district's leaid

Implementation: `api.py::_resolve_level`

### If you're modifying school name matching

Read `docs/name-matching.md` first. Key constraints:
- Fuzzy matching is deliberately avoided (proven wrong)
- NOISE tokens are narrow — level words are kept (MA names schools town+level)
- `MUST_NOT_MATCH` test cases matter most
- Changes must keep `scripts/test_school_match.py` green

Implementation: `scripts/school_match.py`

### If you're adding a state

Read `docs/state-MA.md` "Adding another state" section. The pipeline is
state-agnostic except for the rating source. You need:
1. A state test score download script
2. Proficiency-to-rating thresholds calibrated against a GS overlap set
3. A `docs/state-XX.md`

### If you're modifying the data pipeline

Read `scripts/CLAUDE.md` for pipeline internals. Key point: three upstream
sources fail in ways that produce silently incomplete data — do not
"simplify" the error handling away.

### Storage (`db.py`)

One SQLite file, three roles:
- **TTL cache** (`lookups` table) — degrades to live fetch on error, never raises
- **School directory** (`schools` table) — keyed by `ncessch`
- **School ratings** (`school_ratings` table) — refreshes on a different cadence;
  `source='manual'` never expires; `source='not-rated-*'` never expires

Source priority: `manual`/`not-rated-*` (3) > `greatschools`/`mcas`/`niche` (2) > `cityspire-2020` (1).
Equal-rank writes: last write wins.

## Data & git

`data/`, `cache/`, `output/` are all gitignored and fully regenerable.
Never commit them. `us_cities.csv` IS committed (regenerated by
`scripts/build_cities.py`).
