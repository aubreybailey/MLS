# School-Aware Rental Search App

Standalone rental search tool with school district ratings. Includes both a web UI (Streamlit) and CLI for batch processing.

## Quick Start

### Docker (Recommended)

Data is downloaded automatically during build (~420MB Census boundary data):

```bash
# Build image (first time takes a few minutes to download data)
docker-compose build

# Start web UI
docker-compose up
# Open http://localhost:8501

# Or run CLI for batch/scripted searches
docker-compose run --rm cli "Providence, RI" --limit 50
docker-compose run --rm cli "Austin, TX" --output austin_rentals
```

### Local (Conda)

```bash
# Create conda environment
./setup.sh
# Or manually: conda env create -f environment.yml

conda activate rental-search

# Download boundary data (first time only, ~420MB)
./download_data.sh

# Run web UI
streamlit run web.py
# Open http://localhost:8501

# Or run CLI
python search.py "Providence, RI"
python search.py "Seattle, WA" --output seattle --limit 100
```

## Components

### Web UI (`web.py`)

Interactive Streamlit app with:
- Location search box
- Real-time filtering (price, beds, school rating, flags)
- Interactive OpenStreetMap with color-coded markers
- Sortable data table
- CSV download

### CLI (`search.py`)

Batch-friendly command-line tool:

```
python search.py "City, ST" [options]

Options:
  --output, -o NAME     Output filename base (auto-generated if omitted)
  --limit, -n NUM       Max listings to fetch (default: 50)
  --min-beds NUM        Minimum bedrooms filter
  --max-price NUM       Maximum monthly rent filter
  --tsv                 Output TSV instead of CSV
  --no-map              Skip HTML map generation
  --quiet, -q           Suppress progress output
```

**Output files:**
- `{location}_rentals.csv` - Data with school ratings and flags
- `{location}_rentals.html` - Standalone interactive map (no server needed)

## Daily Notifications (ntfy)

Save a search and get pushed only the **new** matching listings each day via
[ntfy](https://ntfy.sh).

**Create a saved search:** set your location + filters in the web UI, pick an
ntfy topic, and click **🔔 Create notification**. It's written to
`notify/saved_searches.json` (you can also edit that file directly — see
`notify/saved_searches.example.json`).

**Point at your ntfy server:** the `NTFY_SERVER` env var (default
`http://192.168.1.4`, default port). Set it in a `.env` file or the environment.

**Run the notifier** (pushes new listings since the last run; first run seeds
silently):

```bash
NTFY_SERVER=http://192.168.1.4 docker compose run --rm notify
docker compose run --rm notify --dry-run   # preview, send nothing
docker compose run --rm notify --list      # list saved searches
```

**Schedule it daily** with cron on your server:

```cron
# 8am daily
0 8 * * * cd /path/to/MLS && NTFY_SERVER=http://192.168.1.4 docker compose run --rm notify >> notify/notify.log 2>&1
```

Subscribe to your topic in the ntfy app (or `http://192.168.1.4/<topic>`). The
`notify` service uses host networking so it can reach an ntfy server on your LAN.
Dedup state lives in `notify/notify_state.json` (a listing is "new" the first time
its URL is seen).

## Local Cache (`cache/schools.db`)

External lookups are cached in SQLite (`db.py`) and shared by the web UI, CLI,
and the notify cron, so repeat searches don't re-scrape:

| Namespace | What | TTL |
|-----------|------|-----|
| `ratings` | GreatSchools ratings per ~1km cell | 90 days |
| `towns` | Overpass town discovery per center+radius | 365 days |
| `town_zip` | Town -> ZIP resolution | 365 days |

A warm search runs ~6x faster than a cold one (40s -> 7s for 8 hits at 15mi).
Failed lookups are never cached. Inspect with `python db.py`; reset by deleting
`cache/schools.db`. Override the location with `SCHOOLS_DB`.

## Attendance Zones (which elementary school an address actually feeds into)

School **district** boundaries (TIGER) are not the same as school **attendance
zones**. 74% of Massachusetts districts have more than one school (5.5 on
average), so knowing the district doesn't tell you the assigned school.

**Nearest school is not a usable substitute.** Measured against real attendance
zones across 3,048 sampled points in MA multi-school districts, picking the
nearest school gives the **wrong school 43.6% of the time** — zones follow bus
routes, rivers, highways and enrollment balancing, not distance.

So the app uses real zones where they exist and says so plainly where they
don't. Build the layer with:

```bash
python scripts/build_attendance_zones.py --state MA   # downloads ~557MB
docker compose run --rm geopackage                     # (districts, separate)
```

**`elem` is never null and never optimistic.** When the assigned school is
unknown, it reports the *worst* school in the district rather than an average or
a guess, so `--min-elem 7` means "no school this address could be assigned to
rates below 7" — a guarantee you can filter on. `elem_best` shows the other end
of the range.

| `elem_source` | `elem` is | Certainty |
|---|---|---|
| `zoned` | the assigned school's own rating | exact |
| `district-sole` | the district's only school | exact |
| `district-min` | the **lowest**-rated school in the district (`elem_best` = highest) | floor |
| `zoned-unrated` | area average; school known but GreatSchools has no rating | unknown |
| `area-avg` | ~3mi area average — last resort, not a bound | unreliable |

| Column | Meaning |
|---|---|
| `elem_school` | The assigned school. Blank when it can't be determined. |
| `elem_best` | Best case when ambiguous; blank when `elem` is exact. |
| `elem_confirm` | Plain-language caveat, empty when exact. |

Note that `area-avg` is genuinely unreliable, not merely imprecise: a 3-mile
radius pulls in *other districts'* schools. Northborough listings showed
6.6–7.8 that way, below the true district floor of 7.0 — it wasn't even a bound.

### One-command setup per state

`scripts/setup_state.py` runs every layer in dependency order. Each step is
idempotent and resumable, so an interrupted or partial run is fixed by running
it again.

```bash
python scripts/setup_state.py --state MA          # everything
python scripts/setup_state.py --state MA --only ratings
python scripts/setup_state.py --state MA --dry-run
```

| Step | Produces |
|---|---|
| `boundaries` | Census TIGER district shapefiles |
| `geopackage` | `data/school_districts.gpkg` (indexed) |
| `zones` | `data/attendance_zones.gpkg` (NCES SABS) |
| `schools` | `schools` table in `cache/schools.db` |
| `ratings` | `school_ratings` table |

It finishes with a verification summary — school counts, rating coverage, and
how many districts have a *complete* worst-case floor — so partial builds are
visible rather than silent.

Name matching between GreatSchools and NCES has regression tests:
`python scripts/test_school_match.py` (32 cases).

### Schools table

`cache/schools.db` holds an NCES-keyed school directory plus per-school ratings,
so a zone lookup (which yields an `ncessch`) lands on an exact rating instead of
a map-tile average.

```bash
python scripts/build_schools_table.py --state MA        # 1,862 MA schools
python scripts/backfill_school_ratings.py --location "Northborough, MA" \
       --radius 8 --limit 100 --level elementary
```

The backfill walks outward from a point, queries GreatSchools once per ~1km
cell, and matches each result back to an NCES school id. Matching is
deliberately conservative (`scripts/school_match.py`): ambiguous names are
skipped, because attaching the wrong school's rating is worse than none.
Hand-entered ratings (`source='manual'`) never expire.

Why per-school matters — Northborough's four elementaries:

| School | Rating |
|---|---|
| Fannie E Proctor | 9.0 |
| Marguerite E Peaslee | 8.0 |
| Lincoln Street | 7.0 |
| Marion E Zeh | 7.0 |

A 2-point spread inside one district, which a district-level or area-average
number cannot express.

**Source limitations — read these.** Zones come from the NCES School Attendance
Boundary Survey (SABS), which was experimental and **discontinued after 2015-16**,
so boundaries are ~10 years old and districts that have since redrawn them will
be wrong (we can't detect which). Participation was voluntary: in MA only
**192 of 322** districts responded (~60%). Boston, Lowell, Lawrence, Quincy,
Shrewsbury and Northborough have **no zones at all** and will always show
`*confirm elementary`.

## Warning Flags

Listings are automatically flagged:

| Flag | Meaning |
|------|---------|
| `UNIT` | Unit in multi-family building |
| `OLD(Xd)` | On market >60 days (stale) |
| `SQFT?` | Suspicious square footage |
| `MULTI` | Multi-family property style |
| `ROOM` | Room rental keywords detected |
| `PRICE?` | Unrealistic price per sqft |

## Map Legend

Marker colors indicate elementary school rating:
- 🟢 **Dark Green** (8+) - Excellent
- 🟢 **Light Green** (7-8) - Good
- 🟡 **Orange** (6-7) - Average
- 🔴 **Tomato** (5-6) - Below average
- 🔴 **Crimson** (<5) - Poor
- ⚫ **Gray** - No data

Dashed border = listing has warning flags

## Data Requirements

Requires Census TIGER school district shapefiles in `data/`:

```
data/
├── tl_2023_*_unsd.shp   # Unified school districts
├── tl_2023_*_elsd.shp   # Elementary districts
└── tl_2023_*_scsd.shp   # Secondary districts
```

These cover all 50 US states. `./download_data.sh` fetches them and then builds
`data/school_districts.gpkg` — a single GeoPackage (SQLite + R-tree spatial
index) that the lookup uses instead of scanning per-state shapefiles.

Build or rebuild it on its own:

```bash
python scripts/build_geopackage.py [--force]   # local conda
docker compose run --rm geopackage [--force]   # docker
```

Note that not every state has every district type — most have only `unsd`
(unified). New England is the main exception: Massachusetts districts live in
`elsd`/`scsd`, so those types are required for MA lookups to resolve.

Once the GeoPackage exists the `tl_2023_*` shapefiles are only needed to rebuild
it, and can be deleted to reclaim ~237MB.

## Project Structure

```
app/
├── web.py              # Streamlit web UI
├── search.py           # CLI tool
├── scripts/
│   ├── school_district_lookup.py
│   └── greatschools_scraper.py
├── data/               # Census TIGER boundaries (~420MB, downloaded)
├── output/             # CLI output directory
├── environment.yml     # Conda environment (Docker + local)
├── setup.sh            # Local conda setup
├── download_data.sh    # Download Census boundary data
├── Dockerfile          # Includes data download in build
├── docker-compose.yml
└── README.md
```

## Examples

### Web UI

1. Start: `docker-compose up`
2. Open http://localhost:8501
3. Enter "Austin, TX" and click Search
4. Filter by price, beds, school rating
5. Click "Download CSV" when done

### CLI Batch Search

```bash
# Search multiple cities
for city in "Austin, TX" "Seattle, WA" "Denver, CO"; do
  docker-compose run --rm cli "$city" --limit 100 --quiet
done

# Results in ./output/
ls output/*.csv output/*.html
```

### Sample Output

```
$ python search.py "Providence, RI" --limit 20

Searching rentals in Providence, RI...
Found 103 raw listings
Processing 20 listings after filters...

Saved 20 listings to providence_ri_rentals.csv
Saved map to providence_ri_rentals.html

======================================================================
RESULTS: 20 listings
======================================================================

Price range: $900 - $3,280
Clean listings (no flags): 8/20
Avg elementary rating: 6.3/10

TOP 10 BY ELEMENTARY SCHOOL RATING:
----------------------------------------------------------------------
  $2,700  4bd  Elem: 6.5  126 Lancashire St # 2               ⚠️UNIT|MULTI
  $1,775  2bd  Elem: 6.4  53 Sterling Ave Unit 1/23           ⚠️UNIT
  ...
```

## MCP Servers (Optional)

The parent `mls/` directory contains MCP (Model Context Protocol) servers for Claude integration. These are **not required** for the standalone app but may be useful for conversational workflows:

### Python MCP (`../mcp_server.py`)

Provides Claude with school-focused tools:
- `lookup_school_district` - Address → school district lookup
- `find_elementary_schools` - Find nearby schools by name/distance
- `full_school_report` - Comprehensive school report for an address
- `annotate_addresses` - Batch annotate addresses with school data
- `search_rentals_with_ratings` - Conversational rental search

### TypeScript MCP (`../MLSMCP/`)

33 property data tools (many still stubs):
- Listings search, market stats, comps
- Agent/broker lookup
- Mortgage calculators

To use with Claude Desktop, add to `~/.claude.json`:
```json
{
  "mcpServers": {
    "school-lookup": {
      "command": "python3",
      "args": ["/path/to/mls/mcp_server.py"]
    }
  }
}
```
# MLS
