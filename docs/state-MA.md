# Massachusetts

State-specific notes for building and using the MA school data.

## Rating source: MCAS

Massachusetts publishes school-level test results through the
[MCAS](https://educationtocareer.data.mass.gov/) (Massachusetts
Comprehensive Assessment System). The build pipeline downloads per-school
"Meeting or Exceeding Expectations" percentages for ELA and Math, averages
them, and converts to a 1-10 rating.

### Proficiency-to-rating thresholds

Calibrated against 1,566 schools that have both a GreatSchools rating and
MCAS results. Thresholds are midpoints between adjacent GS-tier median
proficiencies.

| Proficiency | Rating | | Proficiency | Rating |
|---|---|---|---|---|
| < 10.5% | 1 | | 38–46.5% | 6 |
| 10.5–17% | 2 | | 46.5–55.5% | 7 |
| 17–22% | 3 | | 55.5–64.5% | 8 |
| 22–29.5% | 4 | | 64.5–73% | 9 |
| 29.5–38% | 5 | | >=73% | 10 |

### Accuracy vs GreatSchools

Measured on the 1,566-school calibration set
(run `scripts/analyze_mcas_loss.py --state MA` to reproduce):

| Metric | Value |
|---|---|
| MAE | 0.82 |
| RMSE | 1.16 |
| Exact match | 37.9% |
| Within +/-1 | 83.7% |
| Within +/-2 | 97.3% |
| Mean delta | -0.04 (near zero bias) |
| MAE 95% CI | [0.78, 0.86] |

Per-GS-tier breakdown shows no systematic bias at the extremes:

| GS tier | N | MAE | Mean delta | Exact | +/-1 |
|---|---|---|---|---|---|
| 1 | 33 | 0.42 | +0.42 | 63.6% | 93.9% |
| 2 | 60 | 1.02 | +0.45 | 30.0% | 75.0% |
| 3 | 134 | 1.19 | +0.02 | 19.4% | 67.2% |
| 4 | 210 | 1.02 | -0.05 | 31.4% | 70.5% |
| 5 | 246 | 0.91 | -0.19 | 31.3% | 82.5% |
| 6 | 266 | 0.82 | -0.14 | 38.3% | 86.1% |
| 7 | 208 | 0.72 | +0.02 | 40.4% | 88.9% |
| 8 | 197 | 0.70 | +0.06 | 40.6% | 89.8% |
| 9 | 142 | 0.61 | -0.03 | 45.8% | 96.5% |
| 10 | 69 | 0.35 | -0.35 | 78.3% | 94.2% |

### District-floor impact

The metric that matters for users — how often the worst-case floor changes:

**Elementary** (134 districts with 2+ overlap schools):
- Floor unchanged: 47.8%
- Floor within +/-1: 89.6%
- Floor higher by 2+ (danger): 11 districts (all exactly +2)

The 11 danger districts are small suburban districts where one school
diverges. None has a catastrophic shift. See `analyze_mcas_loss.py` output
for the full list.

### Coverage

| Category | Count |
|---|---|
| Total MA schools | 1,862 |
| MCAS can rate | 1,597 (97.9% of ratable) |
| Tagged PK/K-2 (no testing) | 166 |
| Tagged alt/virtual | 67 |
| No MCAS data | 35 |

## SABS attendance zone coverage

196 of 403 MA districts have NCES SABS attendance boundaries (49%). Notable
districts **without** SABS zones: Boston, Lowell, Lawrence, Quincy,
Shrewsbury, Northborough. These fall back to `district-min`.

The SABS survey was conducted in 2015-16 and has not been repeated. Zone
boundaries that have changed since then will be silently wrong.

## Building

```bash
python scripts/setup_state.py --state MA              # MCAS-only (default)
python scripts/setup_state.py --state MA --non-free    # + GreatSchools
```

The MCAS step (`backfill_mcas.py`) uses the NCES CCD `seasch` field to
crosswalk NCES school IDs to MA DESE org codes, then fetches proficiency
data from the DESE Socrata portal.

## Adding another state

To add state XX, you need:

1. A state test score data source equivalent to MCAS (most states publish
   school-level proficiency data through their department of education).
2. A script like `backfill_mcas.py` that downloads and converts the data.
3. A `step_XX` function in `setup_state.py` (or generalize `step_mcas` to
   dispatch by state).
4. Proficiency-to-rating thresholds calibrated for that state's test, ideally
   against a GreatSchools overlap set.
5. A `docs/state-XX.md` documenting the above.

The rest of the pipeline (boundaries, geopackage, zones, schools, classify)
is already state-agnostic.
