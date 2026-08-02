# School-Aware Rental Search

Find rentals filtered by the quality of the school an address actually feeds
into — not the district average, and not the nearest school.

Built on **public government data** — Census boundaries, the NCES school
directory, federal attendance zones, and state test scores. The database is
fully reproducible and redistributable. An optional `--non-free` flag adds
scraped GreatSchools ratings for broader coverage.

## Why this exists

Most rental sites show a school rating that is some average of whatever is
nearby. That number is close to useless:

- **Most districts have multiple schools** that differ by 2+ rating points.
- **A radius average crosses district lines**, blending schools the address
  can't access.
- **"Nearest school" is wrong 43.6% of the time.** Zones follow bus routes,
  rivers, and enrollment balancing — not distance.

This app resolves the actual assigned school where knowable, reports a
guaranteed worst-case floor where it isn't, and says plainly which one
you're looking at. See [how the rating works](docs/rating-resolution.md).

## Quick start

```bash
python scripts/setup_state.py --state MA    # build the data (once per state)
docker compose up -d                        # web UI at http://localhost:8501
```

CLI:

```bash
docker compose run --rm cli "Northborough, MA" \
    --school-level elementary --min-rating 7 --radius 15
```

## Building the data

```bash
python scripts/setup_state.py --state MA              # government data only
python scripts/setup_state.py --state MA --non-free    # + GreatSchools
```

The default pipeline downloads district boundaries (Census TIGER), school
locations (NCES CCD), attendance zones (NCES SABS), and state test scores
(MCAS for MA) — all public government sources. Each step is idempotent and
resumable. See [data sources](docs/data-sources.md) for details and upstream
failure modes.

`--non-free` adds a GreatSchools rating scrape. The scraped data can't be
redistributed, so a `--non-free` build is for personal use only.

## Data sources

| Source | What | Agency |
|---|---|---|
| [Census TIGER](https://www2.census.gov/geo/tiger/TIGER2023/) | District boundary polygons | US Census Bureau |
| [NCES CCD](https://educationdata.urban.org/documentation/) | School directory | Dept. of Education |
| [NCES SABS](https://nces.ed.gov/programs/edge/SABS) | Attendance zone boundaries | Dept. of Education |
| State test scores | Proficiency -> 1-10 rating | State education agency |

Full provenance details: [docs/data-sources.md](docs/data-sources.md)

## How the rating works

| `*_source` | What the rating is | Certainty |
|---|---|---|
| `zoned` | The address's assigned school | exact |
| `district-sole` | The district has one school at this level | exact |
| `district-min` | The worst-rated school in the district | a floor |
| `area-avg` | ~3 mi radius average — last resort | not a bound |

**The rating is never optimistic and never null.** `--min-rating 7` means
no school this address could be assigned to rates below 7.

Full explanation: [docs/rating-resolution.md](docs/rating-resolution.md)

## Filters and output

| Filter | Notes |
|---|---|
| Location + radius | 0 = city only; >0 expands to nearby towns |
| Target hits | Quota of passing listings to collect |
| Max price, Min beds, Min sqft | |
| School level + Min rating | Applied to the floor rating |

| Flag | Meaning |
|---|---|
| `UNIT` | Unit in a multi-family building |
| `OLD(Xd)` | On market >60 days |
| `SQFT?` / `PRICE?` | Suspicious square footage or price |
| `MULTI` / `ROOM` | Multi-family property or room rental |

Map markers are colored by rating (green >= 8, crimson < 5, grey = no data).

## Daily notifications

```bash
NTFY_SERVER=http://192.168.1.4 docker compose run --rm notify
```

Set filters in the web UI, pick an ntfy topic, click Create notification.
The notifier pushes only new listings since the last run.

## Project structure

```
web.py              Streamlit UI
search.py           Quota-fill orchestration, CLI, map
api.py              Data-access facade (MCP-wrappable)
db.py               SQLite: caches, schools, ratings
notify.py           Saved searches -> ntfy
scripts/            Build pipeline + analysis tools
docs/               Deep dives (see below)
```

## Documentation

| Doc | What's in it |
|---|---|
| [Rating resolution](docs/rating-resolution.md) | Source precedence, the never-optimistic invariant, output columns |
| [Data sources](docs/data-sources.md) | Every upstream source, failure modes, provenance taxonomy |
| [Name matching](docs/name-matching.md) | Why not fuzzy, the tier algorithm, test cases |
| [Zone inference](docs/zone-inference.md) | Voronoi validation, MLS sampling, inferred zone polygons |
| [Massachusetts](docs/state-MA.md) | MCAS calibration, accuracy vs GreatSchools, coverage |

Implementation plans: `docs/mcas-first-foss-plan.md`, `docs/inferred-zones-plan.md`

## Adding a state

The pipeline is state-agnostic except for the rating source. To add a state:

1. Find the state's published school-level test proficiency data.
2. Write a backfill script (see `scripts/backfill_mcas.py` as a template).
3. Calibrate proficiency-to-rating thresholds against a GreatSchools overlap set.
4. Add `docs/state-XX.md`.

Everything else (boundaries, school directory, zones, classify) already works
for all states.

## License

CC BY-NC-SA 4.0. Government data in the default build is public domain.
See [LICENSE](LICENSE).
