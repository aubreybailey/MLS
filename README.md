# School-Aware Rental Search

Find rentals filtered by the quality of the school an address actually feeds
into — not the district average, and not the nearest school.

Streamlit web UI plus a CLI, backed by Realtor.com listings, Census school
district boundaries, NCES attendance zones, and GreatSchools ratings.

---

## Why this exists

Most rental sites show a school rating that is some average of whatever is
nearby. That number is close to useless for deciding where to live, because:

- **74% of Massachusetts districts have more than one school** (5.5 on average),
  and the schools within one district can differ by 2+ rating points. In
  Northborough they run 9.0, 8.0, 7.0, 7.0.
- **A radius average crosses district lines.** Listings in Northborough
  averaged 6.6–7.8 that way — *below* the worst school the address could
  actually be assigned to. It wasn't a bad estimate; it wasn't an estimate of
  anything.
- **"Nearest school" is not a fix.** Measured against real attendance
  boundaries across 3,048 sampled points in multi-school districts, the nearest
  school is the **wrong school 43.6% of the time**. Zones follow bus routes,
  rivers and enrollment balancing, not distance.

So this app resolves the actual assigned school where that is knowable, reports
a guaranteed floor where it isn't, and says plainly which one you're looking at.

---

## Quick start

```bash
systemctl --user start podman.socket   # rootless podman only
docker compose up -d                   # web UI at http://localhost:8501
```

If it fails to start, `docker compose rm -f` first.

First run needs the school data built (once, per state):

```bash
python scripts/setup_state.py --state MA
```

CLI:

```bash
docker compose run --rm cli "Providence, RI" --limit 25
docker compose run --rm cli "Northborough, MA" \
    --school-level elementary --min-rating 7 --min-sqft 900 --radius 15
```

Local (conda) instead of Docker:

```bash
./setup.sh && conda activate rental-search
streamlit run web.py
```

---

## How the school rating works

Pick a level (elementary / intermediate / high) and a minimum rating. The
filter applies to that level, and every level resolves the same way:

| `*_source` | What the rating is | Certainty |
|---|---|---|
| `zoned` | The address's **assigned school**, from NCES attendance boundaries | exact |
| `district-sole` | The district has one school at this level | exact |
| `district-min` | The **worst-rated** school in the district (`*_best` shows the best) | a floor |
| `zoned-unrated` | School identified, but GreatSchools has no rating for it | unknown |
| `area-avg` | ~3mi radius average — last resort | **not a bound** |

The important property: **the rating is never optimistic and never null.** When
the assigned school is unknown, you get the district's worst school, so
`--min-rating 7` means *"no school this address could be assigned to rates below
7"* — something you can safely filter on, rather than an estimate that might be
off in either direction.

Columns carrying this:

| Column | Meaning |
|---|---|
| `elem` / `mid` / `high` | Best available rating for that level |
| `*_school` | The assigned school, when known |
| `*_best` | Best case when ambiguous; blank when exact |
| `*_confirm` | Plain-language caveat, e.g. `not exact - worst case (7.0-9.0 across district), 2 unrated` |

A `, N unrated` suffix means some district schools have no rating, so the floor
itself is incomplete — the true worst case could be lower.

---

## Building the data

`scripts/setup_state.py` runs every layer in dependency order. Each step is
idempotent and resumable; an interrupted run is fixed by running it again.

```bash
python scripts/setup_state.py --state MA
python scripts/setup_state.py --state MA --only ratings
python scripts/setup_state.py --state MA --dry-run
```

| Step | Source | Produces |
|---|---|---|
| `boundaries` | Census TIGER | `data/tl_2023_*` shapefiles |
| `geopackage` | the above | `data/school_districts.gpkg` (one indexed file) |
| `zones` | NCES SABS | `data/attendance_zones.gpkg` |
| `schools` | NCES CCD | `schools` table in `cache/schools.db` |
| `ratings` | GreatSchools | `school_ratings` table |

It ends with a verification summary, so a partial build is visible rather than
silent:

```
district GeoPackage    273MB
attendance zones       8MB
schools                1862
elementary rated       781/1001 (78%)
districts fully rated  126/269
```

Disk: ~555MB of boundary data, ~2MB database. All of it is regenerable and none
is in git.

### Why this is a script and not a list of steps

Three of those sources fail in ways that produce **silently incomplete data**:

- **census.gov rate-limits with HTTP 429.** The original download script read
  that as "this state has no such district type" and, under `set -e`, aborted
  the whole run at the first missing state. The result was every Northeast
  secondary district file missing, with no error — Northborough had no high
  school district at all.
- **GreatSchools caps results at 25 per page**, regardless of radius. Boston has
  109 schools, so one query saw a quarter of them, and widening the radius made
  it *worse*.
- **GreatSchools answers an empty search with HTTP 404**, not an error, so rural
  points returned nothing.

All three are handled (retry with backoff, pagination, radius escalation), but
they're the reason reproducing this by hand is a bad idea.

---

## Filters

Set before searching — they drive the scan, which keeps going until it collects
the target number of listings that **pass** (or exhausts the pool), rather than
fetching N and filtering afterward.

| Filter | Notes |
|---|---|
| Location + radius | 0 = city only; >0 expands to nearby towns nearest-first, only until the quota fills |
| Target hits | Number of *passing* listings to collect |
| Max price, Min beds, Min sqft | Listings with no sqft are excluded when min sqft is set |
| School level + Min rating | See above |
| Hide flagged / Hide UNIT | Warning flags below |

### Warning flags

| Flag | Meaning |
|---|---|
| `UNIT` | Unit in a multi-family building |
| `OLD(Xd)` | On market >60 days |
| `SQFT?` | Suspicious square footage |
| `MULTI` | Multi-family property style |
| `ROOM` | Room-rental keywords detected |
| `PRICE?` | Unrealistic price per sqft |

Map markers are colored by rating (green ≥8 → crimson <5, grey = no data);
a dashed border means the listing has flags.

---

## Daily notifications (ntfy)

Set filters in the web UI, pick a topic, click **🔔 Create notification** — it
saves to `notify/saved_searches.json`. The notifier pushes only listings that
are **new since the last run** (first run seeds silently).

```bash
NTFY_SERVER=http://192.168.1.4 docker compose run --rm notify
docker compose run --rm notify --dry-run    # preview, send nothing
docker compose run --rm notify --list
```

```cron
0 8 * * * cd /path/to/MLS && docker compose run --rm notify >> notify/notify.log 2>&1
```

Dedup state lives in `notify/notify_state.json`. The `notify` service uses host
networking so it can reach an ntfy server on your LAN.

---

## Data sources and their limits

| Source | Used for | Limitation |
|---|---|---|
| Realtor.com (via `homeharvest`) | Listings | `nearby_schools` exists but is never populated |
| Census TIGER 2023 | District boundaries | Solid; verified against NCES with zero substantive disagreement across MA |
| **NCES SABS** | **Attendance zones** | **Discontinued after 2015-16, and voluntary — only 192/322 MA districts (60%)** |
| NCES CCD | School directory | Public schools only; private schools won't match |
| GreatSchools | Ratings | Scraped; ~78% of MA elementary schools matched |

**The SABS limitation is the one that matters.** Zones are ~10 years old, so a
district that has redrawn boundaries since will be wrong and we cannot detect
which. And Boston, Lowell, Lawrence, Quincy, Shrewsbury and **Northborough**
have no zones at all — those always fall back to a district floor and say so.

### Name matching

GreatSchools gives no NCES id, so ratings are linked by name plus geography.
This is deliberately **not** fuzzy matching: on real failures, correct pairs
scored 0.67–0.83 on character similarity while *wrong* pairs scored 0.76–0.86.
`Holland Elementary` vs `Holmes Elementary` — different schools — scored highest
of all at 0.86. The distributions overlap completely, so no threshold works, and
an embedding model would be worse: it compresses away the proper noun that
identifies a school.

Instead: token containment, abbreviation expansion, exact-match preference,
grade-span overlap, and a locally-rare-token fallback. Ambiguity that survives
all of it returns nothing, because attaching the wrong school's rating is worse
than having none.

```bash
python scripts/test_school_match.py     # 32 cases, all from real failures
```

---

## Project structure

```
web.py                  Streamlit UI
search.py               Quota-fill orchestration, DataFrame assembly, CLI, map
api.py                  Data-access facade: every read goes through here
                        (get_schools, get_districts, get_attendance_zone,
                        resolve_school, enrich_listing, get_listings, ...);
                        plain args, JSON-serializable returns -- MCP-wrappable
db.py                   SQLite store: caches, schools, school_ratings
notify.py               Saved searches -> ntfy
scripts/
  setup_state.py            Runs every build step in order
  build_geopackage.py       TIGER shapefiles -> indexed GeoPackage
  build_attendance_zones.py NCES SABS -> attendance zones
  build_schools_table.py    NCES CCD -> schools table
  backfill_school_ratings.py GreatSchools -> school_ratings
  school_district_lookup.py District + attendance zone lookups
  school_match.py           GreatSchools <-> NCES name matching
  greatschools_scraper.py   Ratings scraper (paged)
  test_school_match.py      Matcher regression tests
data/                   Boundary data (~555MB, gitignored, regenerable)
cache/                  schools.db (gitignored, regenerable)
output/                 CLI results (gitignored)
```

---

## Development notes

- **Rebuild every service you changed.** `docker compose build` alone can leave
  `cli` or `notify` on stale code.
- **Don't run concurrent `docker compose` commands** under rootless podman —
  they deadlock, and `docker ps` starts hanging too. Recovery is killing the
  stuck `podman`/`compose` processes.
- **`docker compose build --no-cache`** is the only trustworthy signal when
  advancing a pinned dependency; a cached layer will happily serve the old one.
- `pandas` is held `<3.0.0` because `homeharvest` 0.8.18 requires it.
