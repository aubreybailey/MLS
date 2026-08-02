# MCAS-First FOSS Branch Plan

## Goal

Build the school database from **government sources only**, so the project
(or a fork of it) can be published with clean provenance.  GreatSchools and
Niche ratings stay available as a personal overlay for the author's own use,
but nothing in the shareable build depends on scraped data.

## Current state

| Source | Provenance | Schools | Notes |
|---|---|---|---|
| greatschools | scraped | 1,583 | Primary rating source today |
| mcas | government | 28 | Gap-fills schools GS missed |
| niche | scraped | 15 | Manual one-off, not in pipeline |
| cityspire-2020 | scraped | 3 | Legacy GS dump |
| not-rated-pk | inferred | 166 | PK/K-2, below testing grades |
| not-rated-alt | inferred | 67 | Alt/therapeutic/virtual programs |

Total MA schools: 1,862.  Of those, 1,629 have numeric ratings — all but 28
come from scraped sources.

## Why MCAS can replace GreatSchools

MCAS (Massachusetts Comprehensive Assessment System) is published by the MA
Department of Elementary and Secondary Education.  It reports per-school
"Meeting or Exceeding Expectations" percentages for ELA and Math — the same
underlying data GreatSchools uses for its MA ratings.

The existing `backfill_mcas.py` already converts MCAS proficiency to a 1–10
scale using midpoint-binned thresholds calibrated against 1,566 schools with
both GS and MCAS data.

The conversion thresholds:

| Proficiency | Rating |
|---|---|
| < 10.5% | 1 |
| 10.5–17% | 2 |
| 17–22% | 3 |
| 22–29.5% | 4 |
| 29.5–38% | 5 |
| 38–46.5% | 6 |
| 46.5–55.5% | 7 |
| 55.5–64.5% | 8 |
| 64.5–73% | 9 |
| ≥ 73% | 10 |

## Phase 1: Loss analysis (decision gate)

**Script**: `scripts/analyze_mcas_loss.py`

No production code changes.  Reads the existing DB to quantify what we lose
by substituting MCAS for GS.

### Metrics to compute

**Per-school accuracy** (1,566-school overlap set):
- MAE (mean absolute error)
- Exact match %
- Within ±1 %
- Per-GS-tier breakdown (reveals bias at extremes)

**District-floor impact** (the metric that matters for the app):
- For each district with ≥2 overlap schools, compute `district-min` under GS
  ratings and under MCAS ratings
- `floor_same_pct`: % of districts where the floor doesn't change
- `floor_higher_by_2plus`: districts where MCAS floor is ≥2 points higher
  than GS floor (the **danger case** — user filters in a listing they
  shouldn't)
- Per-level breakdown (elementary/middle/high)

**Bootstrap confidence intervals** (10k resamples) on MAE and floor-change
rate.

### Decision gate

If `floor_higher_by_2plus` exceeds ~5 districts, the thresholds need
recalibration or those districts need manual review before proceeding.  A
floor that's 1 point too low is conservative (acceptable); a floor that's
2+ points too high is optimistic (dangerous).

### Implementation

```python
def load_paired_schools(state):
    """Schools with BOTH GS rating AND MCAS proficiency data.
    
    GS rating: school_ratings where source='greatschools'
    MCAS data: re-run build_crosswalk() + fetch_mcas() from backfill_mcas.py
    (the DB only stores MCAS for previously-unrated schools, so we need the
    raw proficiency for the full overlap set).
    """

def district_floor_impact(pairs, state):
    """For each district: compute min(rating) under GS vs MCAS.
    Replicates db.schools_in_district logic (excludes not-rated-alt).
    Returns per-district deltas and aggregate danger metrics."""
```

## Phase 2: Build mode changes

### `setup_state.py` — add `--foss` flag

When `--foss` is set:
- Skip `ratings` step (GS scrape) and `samples` step (GS zone samples)
- Pipeline becomes: `boundaries → geopackage → zones → schools → mcas → classify`
- Run PK/alt tagging after MCAS step (currently runs inside `ratings`)

### `backfill_mcas.py` — add `--primary` flag

When `--primary`:
- Rate ALL schools with MCAS data, not just unrated ones
- Query changes from `LEFT JOIN ... WHERE r.ncessch IS NULL` to
  `SELECT s.ncessch, s.name FROM schools s WHERE s.state = ?`
- `put_school_rating` still works correctly: `SOURCE_PRIORITY` gives `mcas`
  the same rank (2) as `greatschools`, so in a clean FOSS build every write
  succeeds

### `db.py` — no changes required

`SOURCE_PRIORITY` already has `mcas` at rank 2 (same as `greatschools`).
In a FOSS build MCAS fills everything.  If someone later runs `--only ratings`
to add GS as an overlay, same-rank last-write-wins gives GS the slot.

### `verify()` — add FOSS provenance check

Under `--foss`, assert no scraped-provenance sources exist in the DB:

```python
scraped = conn.execute("""
    SELECT r.source, COUNT(*) FROM school_ratings r
    JOIN schools s ON s.ncessch = r.ncessch
    WHERE s.state = ? AND r.source IN ('greatschools','niche','cityspire-2020')
    GROUP BY r.source""", (state,)).fetchall()
```

## Phase 3: Coverage gap analysis

Incorporated into the analysis script.  For each school without MCAS data,
classify the reason:
- `not-rated-pk`: grade_hi < 3 (PK/K-2, no state testing)
- `not-rated-alt`: alt/therapeutic/virtual program
- Not in CCD crosswalk (school opened after crosswalk year)
- In crosswalk but no MCAS results (tiny enrollment, test exemption)

Report `coverage_pct = mcas_rated / (total - pk - alt)` per level.

## Phase 4: Documentation

### README section

```
## FOSS build (government data only)

    python scripts/setup_state.py --state MA --foss

| Source | What | Provenance |
|---|---|---|
| Census TIGER 2023 | District boundaries | US Census Bureau |
| NCES SABS 2015-16 | Attendance zones | Dept. of Education |
| NCES CCD | School directory | Dept. of Education |
| MA DESE MCAS | Test proficiency | MA state government |

### Adding GreatSchools as a personal overlay

    python scripts/setup_state.py --state MA --only ratings
```

## Phase 5: Testing

```bash
# Phase 1 analysis
python scripts/analyze_mcas_loss.py --state MA

# Phase 2 FOSS build (from scratch)
rm cache/schools.db  # or use a separate DB path
python scripts/setup_state.py --state MA --foss

# Verify
python scripts/test_school_match.py
docker compose build cli
docker compose run --rm cli "Northborough, MA" --limit 3 --no-map
```

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| MCAS floor ≥2 higher than GS in many districts | High | Phase 1 decision gate; recalibrate thresholds |
| MCAS biased at tails (regression to mean) | Medium | Per-tier breakdown reveals it |
| Schools with no MCAS data at all | Low | Already tagged PK/alt; tiny residual |
| Threshold calibration drifts year-over-year | Low | Recalibrate annually when new MCAS released |

## Sequence

1. `scripts/analyze_mcas_loss.py` — run the numbers
2. Review results — go/no-go on the floor-shift danger metric
3. `--foss` flag + `--primary` flag
4. README + CLAUDE.md updates
5. Test full FOSS pipeline end-to-end
