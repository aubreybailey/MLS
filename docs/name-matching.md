# School Name Matching

How GreatSchools school names are linked to NCES school IDs, and why this
is not fuzzy matching.

## The problem

GreatSchools returns school names with no NCES ID. The NCES CCD directory
has the authoritative school list. Linking them requires name matching
across two naming conventions:

- GreatSchools: `"Fales Elementary School"`
- NCES CCD: `"Annie E. Fales Elementary School"`

## Why not fuzzy matching

Character-level similarity (Levenshtein, Jaro-Winkler, etc.) **cannot
separate correct matches from wrong ones** in this domain. Measured on
real MA data:

- Correct pairs scored 0.67–0.83 on normalized similarity.
- **Wrong** pairs scored 0.76–0.86.
- The highest-scoring pair in the sample was `Holland Elementary` vs
  `Holmes Elementary` at 0.86 — **a wrong match** (different schools in
  different towns).

The distributions overlap completely. No threshold produces acceptable
precision. An embedding model would be worse: it compresses away the proper
noun that is the only thing distinguishing one school from another.

## The matching algorithm

Implementation: `scripts/school_match.py`

Matching is deterministic and tiered. Each tier is tried in order; the
first unambiguous match wins.

### Tier 1: Token-set containment

Tokenize both names, normalize (lowercase, strip punctuation), expand
abbreviations (`st` -> `saint`, `elem` -> `elementary`). If one name's
token set is a subset of the other's, it's a match — `"Fales Elementary"`
matches `"Annie E. Fales Elementary School"`.

### Tier 2: Exact normalized

After normalization, if the strings are identical, match.

### Tier 3: Grade-span tiebreak

When multiple candidates survive the above, prefer the one whose grade
range overlaps with the query context (e.g., an elementary search
prefers K-5 over 6-8).

### Tier 4: Locally distinctive token

If one name contains a token that is unique among all candidates in the
search radius, that token identifies the school. `"Coolidge"` in a
district where only one school has that word is a match even if the full
name differs. This handles cases like `"Calvin Coolidge School"` vs
`"Coolidge Elementary"`.

### Ambiguity refusal

If none of the tiers produces a single unambiguous match, the function
returns `None`. **A wrong rating on the wrong school is worse than a
missing one.** The caller (rating backfill) simply leaves that school
unrated rather than guessing.

## Design decisions

**NOISE tokens are deliberately narrow.** Words like `school`, `the`,
`of` are dropped, but level words (`elementary`, `middle`, `high`) are
kept. In Massachusetts, many schools are named `<Town> Elementary` — dropping
`elementary` would make `Northborough Elementary` match
`Northborough Middle`.

**Candidate set is geographically scoped.** Matching runs against schools
near the query point, not the entire state. This prevents `Lincoln
Elementary` in Springfield from matching `Lincoln Elementary` in Boston.

**Two-pass radius for zone samples.** `load_zone_samples.py` tries a tight
4-mile candidate set first, then widens to 8 miles only if no match is
found. A midpoint-between-schools sample can sit ~5 miles from the assigned
school in a large regional district.

## Tests

```bash
python scripts/test_school_match.py     # 32 cases, all from real failures
```

The test suite includes explicit `MUST_NOT_MATCH` cases — these matter more
than the positive cases, because they guard against the kind of false match
that attaches the wrong school's rating. Every change to the matcher must
keep these green.
