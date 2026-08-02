# Rating Resolution

How the app turns a GPS coordinate into a school rating.

## The problem

A rental listing has a latitude and longitude. The question is: if a family
with school-age children moves here, what school will their child attend, and
how good is it?

Most sites answer with a radius average or "nearest school." Both are wrong:

- **Radius averages cross district lines**, blending schools the address
  can't access. In Northborough, MA, the radius average was 6.6–7.8 —
  *below* the worst school the address could actually be zoned into.
- **Nearest school is wrong 43.6% of the time.** Measured against 3,048
  sampled points in multi-school districts using real SABS attendance
  boundaries. Zones follow bus routes, rivers, and enrollment balancing —
  not distance. See `scripts/validate_voronoi.py` for the methodology.

## Resolution precedence

For each school level (elementary / middle / high), the app resolves in
strict precedence order. The first source that produces a result wins.

| `*_source` | What the rating is | Certainty |
|---|---|---|
| `zoned` | The address's **assigned school**, from NCES attendance boundaries | exact |
| `district-sole` | The district has only one school at this level | exact |
| `district-min` | The **worst-rated** school in the district (`*_best` shows the best) | a floor |
| `zoned-unrated` | School identified, but no rating source covers it | unknown |
| `area-avg` | ~3 mi radius average — last resort | **not a bound** |

Implementation: `api.py::_resolve_level`.

## The core invariant

**The rating shown and filtered on is never optimistic and never null.**

When the assigned school is unknown, `district-min` returns the district's
*worst* school. This means `--min-rating 7` guarantees *"no school this
address could be assigned to rates below 7"* — a safe filter, not an
estimate.

Consequences:
- `search.py::_passes` filters on `elem`/`mid`/`high` (the floor values).
  `*_best` is display-only and **must never drive a filter or sort**.
- A wrong zone assignment within the same district is acceptable — the
  floor is still the true district worst case. This is why the app uses
  `district-min` instead of attempting geometric inference when zones are
  unavailable.

## Output columns

| Column | Meaning |
|---|---|
| `elem` / `mid` / `high` | Best available rating for that level (the floor when ambiguous) |
| `*_school` | The assigned school, when known |
| `*_best` | Best case when ambiguous; blank when exact |
| `*_source` | Which resolution tier produced the rating |
| `*_confirm` | Plain-language caveat, e.g. `not exact - worst case (7.0-9.0 across district), 2 unrated` |

A `, N unrated` suffix on `*_confirm` means some district schools have no
rating, so the floor is incomplete — the true worst case could be lower.

## District resolution details

Two facts are wired into the resolution and cannot be changed independently:

1. **TIGER district `GEOID` equals the NCES `leaid`.** This is the join key
   across boundaries, zones, and the schools table. If this invariant breaks
   for a new data vintage, every lookup breaks silently.

2. **High school falls back to the secondary district.** Many states
   (especially New England) have separate elementary and secondary districts.
   The `get_districts()` call returns `{'elementary': leaid, 'secondary': leaid}`
   and `_resolve_level` uses the secondary leaid for the `high` level when
   one exists.

## Excluded schools

Two categories of schools are excluded from floor calculations:

- **`not-rated-pk`**: PK/K-2 schools whose grade range is below state
  testing grades. They structurally cannot be rated and their absence is
  explained, not a gap.
- **`not-rated-alt`**: Alt/therapeutic/virtual/transition programs. A house
  is never automatically zoned into these, so they shouldn't drag down the
  floor.

Both are tagged during the build pipeline and stored with `rating=NULL` in
`school_ratings`.
