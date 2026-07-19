#!/usr/bin/env python3
"""
Match GreatSchools school names to NCES school records.

The two sources name the same school differently -- NCES 'Marion E Zeh' vs
GreatSchools 'Marion E. Zeh Elementary School' -- and GreatSchools gives no
NCES id, so linking them is name-plus-geography. Getting this wrong attaches
one school's rating to another, which is worse than having no rating, so the
matcher is deliberately conservative and returns None when unsure.
"""

# Words that carry no identifying signal and appear inconsistently.
NOISE = {'elementary', 'school', 'schools', 'middle', 'high', 'academy', 'the',
         'jr', 'sr', 'junior', 'senior', 'of', 'at', 'and', 'intermediate',
         'primary', 'upper', 'lower', 'regional', 'public', 'charter'}


def normalize(name: str) -> set:
    """Identifying tokens of a school name."""
    cleaned = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in str(name).lower())
    return {t for t in cleaned.split() if t and t not in NOISE}


def names_match(a: str, b: str) -> bool:
    """True when two names denote the same school.

    Requires the shorter token set to be fully contained in the longer one, so
    'Lincoln Street' matches 'Lincoln Street Elementary' but not 'Lincoln High'.
    Single-token names must match exactly -- 'Peirce' vs 'Pierce Middle' would
    otherwise slip through.
    """
    ta, tb = normalize(a), normalize(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap >= min(len(ta), len(tb)) and overlap > 0


def best_match(gs_name: str, candidates: list, name_key: str = 'name'):
    """Pick the NCES school a GreatSchools name refers to.

    `candidates` should already be geographically plausible (same district or
    within a few miles). Returns None on ambiguity -- if two candidates match
    equally well we cannot tell them apart, and guessing would silently
    mis-attribute a rating.
    """
    hits = [c for c in candidates if names_match(gs_name, c.get(name_key, ''))]
    if len(hits) != 1:
        return None
    return hits[0]
