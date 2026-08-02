# Inferred Attendance Zones — Public GeoPackage Plan

## Goal

Produce an open GeoPackage of inferred elementary school attendance zone
polygons for Massachusetts, derived entirely from government data +
publicly-filed MLS listing metadata.  Publish methodology and per-zone
confidence scores so downstream users can assess fitness for their purpose.

## Why this matters

Attendance zone boundaries are administered by individual school districts.
Many exist only as board-vote minutes, superintendent desk maps, or
municipality GIS layers with no standard format.  The federal SABS survey
(2015-16) collected boundaries voluntarily and covers only ~196 of MA's 403
districts.  No survey has been conducted since.  A freely-available inferred
dataset — even an imperfect one — fills a real civic data gap.

## Approach: MLS-seeded labeled point cloud → constrained polygons

### Data sources

| Source | What | How we use it |
|---|---|---|
| Census TIGER 2023 | District boundary polygons | Spatial container — every zone polygon is clipped to its district |
| NCES CCD | School locations + enrollment | Point seeds for Voronoi prior; name matching targets |
| NCES SABS 2015-16 | Ground-truth zone polygons (196 districts) | Validation + direct use where available |
| Realtor.com MLS (via homeharvest) | `for_sale` listing metadata: GPS coords + "School Information" details | Labeled sample points: (lat, lon) → assigned school name |
| GreatSchools address lookup | "Assigned school in ..." for a lat/lon | Additional labeled points (61 existing samples) |

### Provenance note

The school assignment field in MLS listings is a factual statement about
which government-administered attendance zone a property falls in.  It is
entered by listing agents as part of their MLS disclosure obligations.  The
inferred zone polygons are derived from the spatial pattern of these
government-fact assertions, not from any proprietary zone database.

## Pipeline

### Step 1: Harvest MLS school labels

**Script**: `scripts/harvest_mls_zones.py`

```
for each city in us_cities.csv where state = MA:
    listings = scrape_property(city, listing_type='for_sale',
                               past_days=180, extra_property_data=True)
    for listing in listings:
        extract "School Information" from details[]
        if elementary school name is specific (not "Lottery"/"Choice"/"N/A"):
            emit (lat, lon, school_name, city, district)
```

**Expected yield** (from sampling):
- ~350 MA cities × ~50-100 listings = 17k-35k total listings
- ~30-50% have specific elementary labels = **5k-15k labeled points**
- Rate limiting: ~1 req/sec to Realtor.com, ~6-10 hours total

**Output**: `data/mls_school_labels.csv` — committed, so the expensive
scrape is done once and the rest of the pipeline is reproducible.

### Step 2: Match school names to NCES

**Script**: `scripts/match_mls_to_nces.py`

For each labeled point:
1. Look up which district the point falls in (district GeoPackage)
2. Get NCES elementary schools in that district
3. Run `school_match.best_match(mls_name, candidates)`
4. Accept only unambiguous matches (same conservative policy as
   `store_zone_sample.py`)

**Output**: augmented CSV with `ncessch` column.  Unmatched rows kept with
`ncessch=NULL` for manual review.

Anticipated match challenges:
- Abbreviated names ("Fes" → Framingham Elementary?) — reject, too ambiguous
- Variant spellings ("Hemenway Elem" vs "Hemenway Elementary") — handled by
  existing token-set matching
- Choice districts ("Lottery", "Ranked Choice") — correctly rejected, but
  the detection itself is useful: confirms the district is choice-based

### Step 3: Load into zone_samples table

Merge with existing 61 GreatSchools-sourced samples.  New source tag:
`mls-inferred`.  Add to provenance taxonomy:

```python
'mls-inferred': 'public-filing',  # school assignment from MLS disclosure
```

### Step 4: Build zone polygons

**Script**: `scripts/build_inferred_zones.py`

For each district with ≥ N labeled points per school (N = tunable, start
with 5):

#### Method A: Label-constrained Voronoi (baseline)

1. Compute Voronoi tessellation from NCES school locations, clipped to
   district boundary
2. For each cell, check what fraction of labeled points agree with the
   Voronoi assignment
3. Accept cell as-is if concordance ≥ 80%
4. For low-concordance cells, use the labeled points to redraw the boundary:
   fit a decision boundary between the two label classes

#### Method B: Alpha shapes per label (more data-driven)

1. Group labeled points by matched ncessch
2. Compute alpha shape for each group (concave hull)
3. Clip to district boundary
4. Resolve overlaps by nearest-labeled-point
5. Fill gaps with Voronoi assignment (labeled points are sparse in
   low-density areas)

#### Method C: k-NN classification on a grid

1. Build a grid of points within the district boundary (~100m spacing)
2. For each grid point, classify by k-nearest labeled points (k=5-7)
3. Polygonize the resulting label raster
4. Smooth boundaries with Douglas-Peucker

**Recommendation**: Start with Method A (Voronoi + concordance check) — it
leverages our existing Voronoi code and degrades gracefully when data is
sparse.  Method C is the most accurate but needs denser point coverage.

### Step 5: Confidence scoring

Each zone polygon gets metadata:

| Field | Meaning |
|---|---|
| `ncessch` | NCES school ID |
| `school_name` | School name from NCES directory |
| `leaid` | District ID |
| `sample_count` | Number of labeled points supporting this zone |
| `concordance` | Fraction of points that agree with the polygon assignment |
| `method` | `sabs` / `voronoi-mls` / `voronoi-prior` |
| `confidence` | `high` (SABS or ≥20 concordant points), `medium` (5-19), `low` (Voronoi prior only) |

### Step 6: Validation against SABS

For the 115 SABS districts with multiple elementary schools (the same set
from our Voronoi validation):
1. Build inferred zones using MLS data only (withhold SABS)
2. Sample random points and compare inferred vs SABS assignment
3. Report accuracy improvement over raw Voronoi (baseline: 64.2%)

Target: ≥75% accuracy in districts with ≥10 MLS samples per school.

### Step 7: Package and publish

**Output**: `data/inferred_attendance_zones.gpkg`

Layers:
- `zones_high` — SABS ground truth + high-confidence MLS-inferred
- `zones_medium` — medium-confidence inferred
- `zones_low` — Voronoi prior (no MLS data)
- `metadata` — per-zone confidence scores and sample counts

Companion: `data/inferred_zones_methodology.md` documenting data sources,
matching rules, polygon construction, validation results, and limitations.

## Measured baseline (Voronoi validation, 2026-08-01)

From `scripts/validate_voronoi.py`, 70 multi-school elementary districts:

| Metric | Value |
|---|---|
| Nearest-neighbor accuracy | 64.1% |
| Enrollment-weighted Voronoi | 64.2% |
| Random baseline | 25.0% |
| Points where rating matches (correct school OR same rating) | 77.2% |
| Points where rating differs | 22.8% |
| Rating off by ≥2 | 1,711 / 12,865 (13.3%) |

**By district size**:
- 3-8 schools: 57-79% accuracy (Voronoi works reasonably)
- 9+ schools: 3-23% accuracy (dense urban, geometry fails)
- 2 schools: ~50% (at baseline, boundary is non-obvious)

**Worst districts** (Cambridge, Somerville, Brookline): irregular
politically-drawn zones that no geometric method can infer.  These are also
the districts most likely to have "Lottery"/"Choice" in MLS labels,
correctly signaling that zones don't apply.

## Implementation sequence

1. `scripts/harvest_mls_zones.py` — the big scrape (~6-10 hours)
2. `scripts/match_mls_to_nces.py` — name matching + manual review of failures
3. Extend `zone_samples` table + provenance
4. `scripts/build_inferred_zones.py` — Method A first
5. Validation against SABS holdout
6. Iterate on method if accuracy target not met
7. Package GeoPackage + methodology doc

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| MLS school names too vague/abbreviated | Medium | Conservative matching (reject ambiguous); ~30-50% usable rate already measured |
| Sparse coverage in rural districts | Medium | Fall back to Voronoi prior; label as low confidence |
| Realtor.com rate limiting / blocking | Medium | Throttle to 1 req/sec; scrape once, commit CSV |
| Choice districts have no zones to infer | None | "Lottery"/"Choice" labels correctly identify these; mark district as choice |
| Zone boundaries changed since MLS data | Low | 180-day window; zones change rarely (board votes) |
| Stale SABS data (2015-16) vs current zones | Low | Known limitation; document as "validation is against 2015-16 boundaries" |

## Public value

The final GeoPackage would be, to our knowledge, the most complete open
dataset of MA elementary attendance zones:
- 196 districts from SABS (2015-16, ground truth)
- ~100-150 additional districts from MLS inference (with confidence scores)
- ~50-100 districts marked as choice/single-school (no zones applicable)
- Remaining districts: Voronoi prior with low-confidence flag

Methodology is fully reproducible from the scripts in this repo plus a
Realtor.com scrape (or the committed CSV of labeled points).
