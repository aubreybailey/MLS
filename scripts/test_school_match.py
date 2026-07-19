#!/usr/bin/env python3
"""
Regression tests for GreatSchools <-> NCES name matching.

Every case here came from a real failure. A wrong match silently attaches one
school's rating to another address, which is the worst outcome this app can
produce, so the MUST_NOT_MATCH cases matter more than the coverage ones.

Run:  python scripts/test_school_match.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from school_match import (names_match, normalize, parse_grades,
                          match_by_distinctive_token)

# (greatschools_name, nces_name) pairs that are the same school.
MUST_MATCH = [
    # NCES omits the level word GreatSchools includes
    ('Marion E. Zeh Elementary School', 'Marion E Zeh'),
    ('Marguerite E. Peaslee Elementary School', 'Marguerite E Peaslee'),
    ('Fannie E. Proctor Elementary School', 'Fannie E Proctor'),
    ('Lincoln Street Elementary School', 'Lincoln Street'),
    ('Thompson Elementary School', 'Thompson'),
    ('Peirce School', 'Peirce'),
    # abbreviations
    ("Snowden Int'L High School", 'Snowden International High School'),
    ('East Somerville Community School', 'E Somerville Community'),
    ('Massachusetts Academy for Math and Science', 'Ma Academy for Math and Science'),
    ('Mount Greylock Regional', 'Mt Greylock Reg'),
    # exact
    ('Boston Arts Academy', 'Boston Arts Academy'),
    ('Holland Elementary School', 'Holland Elementary'),
]

# Different schools. These are the dangerous ones: several score HIGHER on
# character similarity than the true matches above, which is why similarity
# scoring (and by extension an embedding model) cannot be used here.
MUST_NOT_MATCH = [
    ('Holland Elementary School', 'Holmes Elementary School'),   # difflib 0.86
    ('Boston Arts Academy', 'Boston Adult Tech Academy'),        # 0.77
    ('Ruth Batson Academy', 'TechBoston Academy'),               # 0.76
    ('Butler Elementary School', 'Missituk Elementary School'),  # 0.76
    ('Amesbury Elementary School', 'Amesbury High'),
    ('Abington High School', 'Abington Middle School'),
    ('Quincy Elementary School', 'Quincy Upper School'),
    ('Lincoln Street Elementary', 'Lincoln High School'),
]

# Middle initials must NOT be expanded as directions.
NORMALIZE_CASES = [
    ('Marion E Zeh', {'marion', 'e', 'zeh'}),
    ('E Somerville Community', {'east', 'somerville', 'community'}),
]

GRADE_CASES = [
    ('PK, K-5', (0, 5)), ('9-12', (9, 12)), ('6-8', (6, 8)),
    ('K-8', (0, 8)), ('2-12 & Ungraded', (2, 12)),
]


# The distinctive-token tier, which catches names containment can't. Dallin is
# the case that regressed silently once: GreatSchools drops the first name
# ('Dallin Elementary School') where NCES keeps it ('Cyrus E Dallin'), so the
# token sets overlap in only one word and containment fails.
def _c(name, lo=0, hi=5):
    return {'name': name, 'grade_lo': lo, 'grade_hi': hi}


DISTINCTIVE_CASES = [
    # (gs_name, gs_grades, candidates, expected_name_or_None)
    ('Dallin Elementary School', 'PK, K-5',
     [_c('Cyrus E Dallin'), _c('Thompson'), _c('Peirce'), _c('Hardy')],
     'Cyrus E Dallin'),
    # 'holland' is locally unique; 'holmes' is a different school entirely
    ('Holland Elementary School', 'PK, K-5',
     [_c('Holmes Elementary School'), _c('Mendell Elementary School')],
     None),
    # Two candidates each own one rare token ('condon', 'james') and both grade
    # spans overlap K-8, so there is no evidence to choose -> refuse.
    ('James F. Condon School', 'PK, K-8',
     [_c('Condon K-8 School', 0, 8), _c('James P Timilty Middle', 6, 8)],
     None),
    # ...but when grades DO discriminate, the tie resolves.
    ('James F. Condon School', 'PK, K-5',
     [_c('Condon K-8 School', 0, 8), _c('James P Timilty Middle', 6, 8)],
     'Condon K-8 School'),
    # a common token alone must never match
    ('Some Elementary School', 'PK, K-5',
     [_c('Other Elementary School'), _c('Third Elementary School')],
     None),
]


def main():
    fails = []

    for gs, grades, cands, expected in DISTINCTIVE_CASES:
        hit = match_by_distinctive_token(gs, cands, gs_grades=grades)
        got = hit['name'] if hit else None
        if got != expected:
            fails.append(f"distinctive({gs!r}) = {got!r}, expected {expected!r}")

    for gs, nces in MUST_MATCH:
        if not names_match(gs, nces):
            fails.append(f"MUST_MATCH failed: {gs!r} ~ {nces!r}")

    for a, b in MUST_NOT_MATCH:
        if names_match(a, b):
            fails.append(f"MUST_NOT_MATCH leaked: {a!r} ~ {b!r}")

    for name, expected in NORMALIZE_CASES:
        got = normalize(name)
        if got != expected:
            fails.append(f"normalize({name!r}) = {sorted(got)}, expected {sorted(expected)}")

    for text, expected in GRADE_CASES:
        got = parse_grades(text)
        if got != expected:
            fails.append(f"parse_grades({text!r}) = {got}, expected {expected}")

    total = (len(MUST_MATCH) + len(MUST_NOT_MATCH) + len(NORMALIZE_CASES)
             + len(GRADE_CASES) + len(DISTINCTIVE_CASES))
    if fails:
        print(f"FAILED {len(fails)}/{total}")
        for f in fails:
            print(f"  {f}")
        return 1
    print(f"ok - {total} cases passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
