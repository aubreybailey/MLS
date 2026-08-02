# Attendance Zone Inference

How the project attempts to infer school attendance zone boundaries where
official data is unavailable, and what we've measured so far.

## The gap

The NCES SABS survey (2015-16) provides attendance zone polygons for only
49% of MA districts. The survey was voluntary and has not been repeated.
For the other 51%, the app falls back to `district-min` (the worst-rated
school in the district) — a safe floor, but less useful than knowing the
actual assigned school.

Attendance zone boundaries are administered by individual school districts.
Many exist only as board-vote minutes, superintendent desk maps, or
municipality GIS layers with no standard format. A freely available
inferred dataset would fill a real civic data gap.

## Approach 1: Geometric inference (Voronoi)

The simplest hypothesis: each school's zone is the set of points closer to
it than to any other school in the district.

### Enrollment-weighted Voronoi

Distance is divided by `sqrt(enrollment)` so larger schools pull from a
wider area — a power-diagram approximation.

```
score = haversine_distance(point, school) / sqrt(enrollment)
```

Lowest score wins.

### Validation results

Tested against SABS ground truth in 70 multi-school elementary districts
(run `scripts/validate_voronoi.py` to reproduce):

| Metric | Value |
|---|---|
| Nearest-neighbor accuracy | 64.1% |
| Enrollment-weighted Voronoi | 64.2% |
| Random baseline | 25.0% |

Enrollment weighting barely helps (+0.1% over unweighted).

**By district size**:

| Schools per district | Districts | Accuracy | Baseline |
|---|---|---|---|
| 3 | 15 | 79.1% | 33.3% |
| 4 | 16 | 59.0% | 25.0% |
| 5 | 13 | 72.3% | 20.0% |
| 6-8 | 10 | 72-75% | 12-17% |
| 9+ | 4 | 3-23% | 9-11% |

Voronoi works reasonably for 3-8 school suburban districts but fails
completely in dense urban areas (Cambridge, Somerville, Brookline) where
zone boundaries are politically drawn.

**Rating impact of misassignment**:

| Metric | Value |
|---|---|
| Points where displayed rating is correct | 77.2% |
| Points where rating differs | 22.8% |
| Rating off by 1 | 1,218 / 12,865 |
| Rating off by 2+ | 1,711 / 12,865 (13.3%) |
| Mean delta (Voronoi - true) | -0.1 |
| Median delta | +1 (slightly optimistic) |

**Conclusion**: Voronoi is dramatically better than random but not reliable
enough to label as `zoned`. The 22.8% wrong-rating rate — with a slight
optimistic bias — means it should not be used to claim certainty about a
specific school assignment.

## Approach 2: MLS-seeded labeled point cloud

Real estate listings on Realtor.com include a "School Information" field
in their details, entered by listing agents as part of MLS disclosure.
This field names the school an address is assigned to.

### What the data looks like

```
#0 9 Upper Joclyn Ave, Framingham
   Elementary School: Ranked Choice      <- choice district, no zone
   
#8 1083 Edgell Rd, Framingham  
   Elementary School: Hemenway Elem      <- specific assignment
   High School: Framingham High School
```

### Measured yield (from sampling 5 MA cities)

| City | Listings (180d) | Usable elem labels | Yield |
|---|---|---|---|
| Newton | 100 | 75 | 75% |
| Waltham | 100 | 71 | 71% |
| Brookline | 100 | 53 | 53% |
| Worcester | 20 | 3 | 15% |
| Cambridge | 100 | 9 | 9% (choice district) |

Choice districts self-identify: labels like "Lottery", "Ranked Choice",
"School Choice" confirm the district has no geographic assignment — useful
metadata even though it produces no zone sample.

### Pipeline (planned)

1. **Harvest**: Scrape `for_sale` listings across all ~350 MA cities
   (180-day window). Expected: 5,000-15,000 labeled GPS points.
2. **Match**: Pipe school names through `school_match.best_match` against
   NCES schools in the listing's district. Conservative matching — reject
   ambiguous.
3. **Accumulate**: Store as `zone_samples` with `source='mls-inferred'`.
4. **Tessellate**: Build zone polygons from labeled point cloud
   (constrained alpha shapes or label-weighted Voronoi, clipped to
   district boundary).
5. **Validate**: Compare against SABS holdout districts.

See `docs/inferred-zones-plan.md` for the full implementation plan.

### Provenance

The school assignment field in MLS listings is a factual statement about
which government-administered attendance zone a property falls in. The
inferred zone polygons would be derived from the spatial pattern of these
assertions, not from any proprietary zone database.

## How the app uses zones today

The current app does NOT use Voronoi inference. It uses:

1. **SABS zones** where available (49% of MA districts) -> `zoned` source
2. **Zone samples** (61 hand-gathered GreatSchools lookups) ->
   `db.nearest_zone_sample` as a point-cloud classifier
3. **District-min** everywhere else -> guaranteed floor

This is deliberately conservative. The `district-min` floor is correct
even when zone assignment is wrong — it's the worst case across the entire
district. Voronoi or MLS inference would only improve the specificity of
the assignment (showing "Lincoln Elementary (8)" instead of "worst case:
7.0-9.0 across district"), not the safety of the floor.
