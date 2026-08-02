# Data Sources

Every upstream data source, what it provides, its failure modes, and its
provenance category.

## Government sources (free pipeline)

These are used in the default build. All are public domain or open-license
government publications.

### Census TIGER 2023 — district boundaries

- **URL**: https://www2.census.gov/geo/tiger/TIGER2023/
- **What**: School district boundary shapefiles (unified, elementary,
  secondary) for all 50 states + DC
- **Script**: `download_data.sh` -> `scripts/build_geopackage.py`
- **Output**: `data/school_districts.gpkg` (~273MB)
- **Provenance**: `census-tiger` / `government`

**Failure mode**: census.gov rate-limits with HTTP 429. A naive download
script reads the 429 as "this state has no such district type" and, under
`set -e`, aborts the entire run. The result was every Northeast secondary
district file missing, with no error. The download script retries with
exponential backoff.

### Census Gazetteer — city/town list

- **URL**: https://www2.census.gov/geo/docs/maps-data/data/gazetteer/
- **What**: Places and county subdivisions with coordinates
- **Script**: `scripts/build_cities.py`
- **Output**: `us_cities.csv` (committed)
- **Provenance**: `government`

New England towns are MCDs (Minor Civil Divisions) absent from the standard
Places file. The city list merges both so towns like Natick and Northborough
appear in autocomplete.

### NCES CCD — school directory

- **URL**: https://educationdata.urban.org/documentation/
- **What**: Every public school: name, location, enrollment, grade range,
  district (leaid)
- **Script**: `scripts/build_schools_table.py`
- **Output**: `schools` table in `cache/schools.db`
- **Provenance**: `nces` / `government`

Key field: `seasch` provides the state-assigned school ID, used to crosswalk
to state test score systems (e.g., MA DESE org codes for MCAS).

### NCES SABS 2015-16 — attendance zone boundaries

- **URL**: https://nces.ed.gov/programs/edge/SABS
- **What**: School attendance zone polygons (which addresses feed into which
  school)
- **Script**: `scripts/build_attendance_zones.py`
- **Output**: `data/attendance_zones.gpkg`
- **Provenance**: `sabs` / `government`

**Critical limitation**: The School Attendance Boundary Survey was voluntary
and has not been repeated since 2015-16. Coverage is partial (196/403 MA
districts, 49%). Districts that have redrawn boundaries since will be
silently wrong.

### MCAS (MA DESE) — state test proficiency

- **URL**: https://educationtocareer.data.mass.gov/resource/i9w6-niyt.csv
- **What**: Per-school "Meeting or Exceeding Expectations" percentages for
  ELA and Math
- **Script**: `scripts/backfill_mcas.py`
- **Output**: `school_ratings` table (source=`mcas`)
- **Provenance**: `mcas` / `government`

Massachusetts-specific. Other states publish equivalent data through their
own departments of education. See `docs/state-MA.md` for calibration details.

## Scraped sources (--non-free pipeline)

These require `--non-free` and produce data that cannot be redistributed.

### GreatSchools — school ratings

- **Script**: `scripts/backfill_school_ratings.py`, `scripts/greatschools_scraper.py`
- **Output**: `school_ratings` table (source=`greatschools`)
- **Provenance**: `greatschools` / `scraped`

**Failure modes**:
- Results are capped at **25 per page** regardless of radius. Boston has
  109 schools, so a single query sees a quarter of them. The scraper
  paginates; widening the radius makes coverage *worse*, not better.
- An empty search returns **HTTP 404**, not an empty result. Rural points
  handle this with radius escalation.
- No NCES ID in results — ratings are linked by name matching
  (see `docs/name-matching.md`).

### GreatSchools address oracle — zone samples

- **Script**: `scripts/store_zone_sample.py`
- **Output**: `zone_samples` table (source=`greatschools-assigned`)
- **Provenance**: `greatschools-assigned` / `scraped`

GreatSchools' "Schools by Address" page returns the school an address is
*assigned* to — current licensed zone data. Requires a real browser (the
assignment is computed by client-side JS; curl gets no assignment). Each
sampled point produces a `(lat, lon) -> school` data point for districts
without SABS coverage.

## Runtime dependencies

Used during live searches, not stored in the database.

### Nominatim / Overpass (OpenStreetMap)

- **What**: Geocoding (location string -> lat/lon) and ZIP code lookup
- **License**: ODbL (open)
- **Rate limit**: 1 req/sec (enforced in `api.py`)

### Realtor.com (via homeharvest)

- **What**: Rental and sale listings
- **License**: Realtor.com terms of service

The `nearby_schools` field exists in listing data but is rarely populated.
The `details[].School Information` field is more useful — it contains
agent-entered school assignments (see `docs/zone-inference.md`).

## Provenance taxonomy

Every `source` value in the database maps to a provenance category
(`db.py::PROVENANCE`):

| Category | Sources | Can redistribute? |
|---|---|---|
| `government` | `nces`, `sabs`, `census-tiger`, `mcas` | Yes |
| `scraped` | `greatschools`, `niche`, `cityspire-2020`, `greatschools-assigned` | No |
| `inferred` | `computed-*`, `not-rated-pk`, `not-rated-alt` | Yes |
| `manual` | `manual`, `manual-research` | Yes |

The default build produces only `government` and `inferred` sources.
`setup_state.py --verify` reports the provenance breakdown.
