#!/bin/bash
# Download Census TIGER school district boundary data (~420MB)
# Run once before first use

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
BASE_URL="https://www2.census.gov/geo/tiger/TIGER2023"

echo "=== School District Boundary Data Download ==="
echo "Target: $DATA_DIR"
echo ""

# State FIPS codes (all 50 states + DC)
STATES="01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56"

# District types
TYPES="UNSD ELSD SCSD"

mkdir -p "$DATA_DIR"

downloaded=0
skipped=0
failed=0

for dtype in $TYPES; do
    echo "--- $dtype boundaries ---"
    for fips in $STATES; do
        file="tl_2023_${fips}_$(echo $dtype | tr '[:upper:]' '[:lower:]').zip"
        shpfile="tl_2023_${fips}_$(echo $dtype | tr '[:upper:]' '[:lower:]').shp"
        url="$BASE_URL/$dtype/$file"
        dest="$DATA_DIR/$file"

        # Skip if shapefile already exists
        if [ -f "$DATA_DIR/$shpfile" ]; then
            skipped=$((skipped + 1))
            continue
        fi

        printf "  %-40s" "$file"
        curl -sL -o "$dest" "$url" 2>/dev/null

        # Verify it's really a zip. A size check isn't enough: the Census serves
        # an 18KB HTML 404 page for state/type combinations that don't exist
        # (e.g. AL has no elementary-only districts), which sails past ">1KB".
        # unzip -t also guards against truncated downloads. Note the explicit
        # failure handling -- under `set -e` a bare failing unzip would abort
        # the entire run at the first missing state.
        if [ -f "$dest" ] && unzip -tq "$dest" >/dev/null 2>&1; then
            if unzip -q -o "$dest" -d "$DATA_DIR" 2>/dev/null; then
                rm -f "$dest"
                downloaded=$((downloaded + 1))
                echo "[OK]"
            else
                rm -f "$dest"
                failed=$((failed + 1))
                echo "[EXTRACT FAILED]"
            fi
        else
            rm -f "$dest"
            failed=$((failed + 1))
            echo "[N/A]"
        fi
    done
done

# Cleanup any remaining zips
rm -f "$DATA_DIR"/tl_2023_*.zip 2>/dev/null

echo ""
echo "=== Summary ==="
echo "  Downloaded: $downloaded new files"
echo "  Skipped:    $skipped (already exist)"
echo "  Unavailable: $failed (not all states have all district types)"

# Count total
shp_count=$(ls -1 "$DATA_DIR"/tl_2023_*_*.shp 2>/dev/null | wc -l | tr -d ' ')
echo ""
echo "Total shapefiles: $shp_count"

# Estimate size
if command -v du &> /dev/null; then
    size=$(du -sh "$DATA_DIR" 2>/dev/null | cut -f1)
    echo "Data directory size: $size"
fi

# Convert to a single indexed GeoPackage. This is what the lookup actually uses;
# without it, lookups fall back to scanning per-state shapefiles (much slower).
echo ""
echo "=== Building GeoPackage ==="
if python -c "import geopandas" 2>/dev/null; then
    python "$SCRIPT_DIR/scripts/build_geopackage.py" --force
else
    echo "geopandas not available in this shell -- skipping."
    echo "Build it later with:"
    echo "  conda activate rental-search && python scripts/build_geopackage.py"
    echo "  # or: docker compose run --rm geopackage"
fi

echo ""
echo "Done! You can now run:"
echo "  python search.py \"Providence, RI\""
