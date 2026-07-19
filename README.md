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

These cover all 50 US states (~420 MB). Mount from parent `mls/data/` or download with:

```bash
cd .. && ./scripts/download_boundaries.sh
```

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
