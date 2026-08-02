# scripts/ — Build Pipeline

## Pipeline overview

`setup_state.py` runs steps in dependency order. Default (free) pipeline:

```
boundaries -> geopackage -> zones -> schools -> mcas -> classify
```

With `--non-free`, adds `ratings` and `samples` between mcas and classify.

Each step is idempotent and resumable. Re-running skips work already done.

## Upstream failure modes

Three sources fail in ways that produce **silently incomplete data**. Do
not simplify or remove the error handling for these:

1. **census.gov HTTP 429** — rate-limits bulk downloads. A naive script
   reads this as "state has no such district type." `download_data.sh`
   retries with exponential backoff.

2. **GreatSchools 25-per-page cap** — regardless of radius. Boston has 109
   schools, so one query sees a quarter. `greatschools_scraper.py`
   paginates. Widening the radius makes coverage *worse*.

3. **GreatSchools HTTP 404 on empty results** — not an error response.
   Rural radius escalation handles it.

## Key scripts

### Data build

| Script | Input | Output | Notes |
|---|---|---|---|
| `build_geopackage.py` | TIGER shapefiles | `school_districts.gpkg` | Merges unified/elem/secondary |
| `build_attendance_zones.py` | NCES SABS ZIP | `attendance_zones.gpkg` | 557MB national download |
| `build_schools_table.py` | NCES CCD API | `schools` table | Paginated API, all states |
| `backfill_mcas.py` | DESE Socrata API | `school_ratings` (mcas) | `--primary` rates all, default gap-fills |
| `backfill_school_ratings.py` | GreatSchools scrape | `school_ratings` (gs) | `--non-free` only |
| `classify_districts.py` | schools + zones | `districts` table | zoned/choice/single/unknown |

### Matching and analysis

| Script | Purpose | Guard |
|---|---|---|
| `school_match.py` | Name matching (see `docs/name-matching.md`) | `test_school_match.py` |
| `analyze_mcas_loss.py` | MCAS vs GS accuracy | Produces `docs/state-MA.md` numbers |
| `validate_voronoi.py` | Zone inference accuracy | Produces `docs/zone-inference.md` numbers |

### Zone sampling

| Script | Purpose |
|---|---|
| `store_zone_sample.py` | Store one GreatSchools address-lookup sample |
| `load_zone_samples.py` | Load committed CSV seed into zone_samples table |

The zone sample CSV lives at `scripts/data/zone_samples.csv` (committed).

## Provenance rules

Every rating source must be registered in `db.py::PROVENANCE` before use.
The provenance taxonomy has four categories:

- `government` — can redistribute
- `scraped` — personal use only
- `inferred` — computed from data we hold, can redistribute
- `manual` — hand-entered, can redistribute

`setup_state.py --verify` reports the provenance breakdown. A default build
should show zero scraped sources.

## Testing

```bash
# Name matching (most important — MUST_NOT_MATCH cases guard against false matches)
python scripts/test_school_match.py

# API contract (needs schools.db)
python scripts/test_api.py
```

No pytest. Plain scripts that exit 0/1.
