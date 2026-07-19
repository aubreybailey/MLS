#!/usr/bin/env python3
"""
Match GreatSchools school names to NCES school records.

The two sources name the same school differently -- NCES 'Marion E Zeh' vs
GreatSchools 'Marion E. Zeh Elementary School' -- and GreatSchools gives no
NCES id, so linking them is name-plus-geography. Getting this wrong attaches
one school's rating to another, which is worse than having no rating, so the
matcher is deliberately conservative and returns None when unsure.
"""

# Words that carry no identifying signal and appear inconsistently between the
# two sources -- NCES writes 'Marion E Zeh' where GreatSchools writes 'Marion E.
# Zeh Elementary School'.
#
# Deliberately narrow -- only truly generic words. Everything else, including
# level words, carries signal: Massachusetts names schools as town+level, so
# 'Amesbury Elementary' / 'Amesbury Middle' / 'Amesbury High' collapse to one
# token if you discard 'elementary'/'middle'/'high' (217 of 681 single-token
# NCES names collide with a neighbour that way). Containment still tolerates
# the asymmetry where NCES omits the level word and GreatSchools includes it:
# {marion,e,zeh} is contained in {marion,e,zeh,elementary}.
NOISE = {'school', 'schools', 'the', 'of', 'at', 'and'}


# Abbreviations the two sources disagree on. These are formatting differences,
# not semantic ones, so expanding them is exact rather than fuzzy.
ABBREV = {
    'intl': 'international', "int'l": 'international',
    'mt': 'mount', 'ft': 'fort',
    'jr': 'junior', 'sr': 'senior',
    'ctr': 'center', 'ctre': 'center', 'cntr': 'center',
    'elem': 'elementary', 'ms': 'middle', 'hs': 'high',
    'tech': 'technical', 'voc': 'vocational', 'reg': 'regional',
    'comm': 'community', 'coop': 'cooperative',
}

# Expanded only in leading position: 'E Somerville' is East Somerville, but the
# E in 'Marion E Zeh' is a middle initial, and 'St' leads Saint Mary's while it
# trails Lincoln St.
LEADING_ABBREV = {
    'e': 'east', 'w': 'west', 'n': 'north', 's': 'south',
    'st': 'saint', 'ma': 'massachusetts',
}


# Applied before tokenizing, since stripping punctuation would otherwise split
# these into meaningless fragments ("int'l" -> "int" + "l").
PRE_SUBS = [("int'l", 'international'), ('intl', 'international'),
            ("nat'l", 'national'), ("ass'n", 'association'),
            ('&', ' and ')]


def _tokens(name: str) -> list:
    text = str(name).lower()
    for a, b in PRE_SUBS:
        text = text.replace(a, b)
    cleaned = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in text)
    return [t for t in cleaned.split() if t]


def normalize(name: str) -> set:
    """Identifying tokens of a school name, with abbreviations expanded."""
    toks = _tokens(name)
    out = []
    for i, t in enumerate(toks):
        if i == 0 and t in LEADING_ABBREV:
            t = LEADING_ABBREV[t]
        else:
            t = ABBREV.get(t, t)
        if t not in NOISE:
            out.append(t)
    return set(out)


def names_match(a: str, b: str) -> bool:
    """Could these two names denote the same school?

    A deliberately permissive *candidate filter*, not a decision: it requires
    the shorter token set to be fully contained in the longer, which tolerates
    NCES omitting a level word ('Marion E Zeh' vs 'Marion E. Zeh Elementary
    School') but still admits genuine near-misses like 'Agawam High' vs 'Agawam
    Junior High'. best_match() is what resolves those, and returns None if it
    cannot. Do not use this alone to decide a rating.
    """
    ta, tb = normalize(a), normalize(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap >= min(len(ta), len(tb)) and overlap > 0


def parse_grades(text: str):
    """Grade span from a GreatSchools label like 'PK, K-5' or '9-12'.

    Returns (low, high) with PK/K as 0, or None when unparseable."""
    t = str(text).lower()
    if not t:
        return None
    nums = [int(n) for n in __import__('re').findall(r'\d+', t)]
    has_k = 'pk' in t or 'k-' in t or t.startswith('k') or ', k' in t
    if not nums:
        return (0, 0) if has_k else None
    lo, hi = min(nums), max(nums)
    if has_k:
        lo = 0
    return (lo, hi)


def grades_compatible(gs_grades: str, cand: dict) -> bool:
    """Do a GreatSchools grade label and an NCES grade span overlap?

    Used only to break ties between candidates that already match by name, so
    'Quincy Elementary' (K-5) wins over 'Quincy Upper School' (6-12) for a
    GreatSchools entry labelled K-5. Unknown grades are treated as compatible
    so missing data never causes a wrong pick -- it just fails to disambiguate.
    """
    a = parse_grades(gs_grades)
    lo, hi = cand.get('grade_lo'), cand.get('grade_hi')
    if a is None or lo is None or hi is None:
        return True
    return a[0] <= hi and lo <= a[1]


def best_match(gs_name: str, candidates: list, name_key: str = 'name',
               gs_grades: str = ''):
    """Pick the NCES school a GreatSchools name refers to.

    `candidates` should already be geographically plausible (same district or
    within a few miles). Returns None on unresolved ambiguity -- guessing would
    silently attach one school's rating to another, which is worse than leaving
    it unrated.

    Ambiguity is resolved only with additional evidence, never by relaxing the
    name rule:
      1. An exact (normalized) name match beats partial ones. 'Boston Latin
         School' matches both 'Boston Latin School' and 'Boston Latin Academy'
         by containment, but only one is exact.
      2. Failing that, grade span must overlap -- separating 'Quincy
         Elementary' from 'Quincy Upper School'.
    """
    hits = [c for c in candidates if names_match(gs_name, c.get(name_key, ''))]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return None

    want = normalize(gs_name)
    exact = [c for c in hits if normalize(c.get(name_key, '')) == want]
    if len(exact) == 1:
        return exact[0]
    pool = exact or hits

    if gs_grades:
        by_grade = [c for c in pool if grades_compatible(gs_grades, c)]
        if len(by_grade) == 1:
            return by_grade[0]

    return None


def match_by_distinctive_token(gs_name: str, candidates: list,
                               name_key: str = 'name', gs_grades: str = ''):
    """Last-resort match on a locally unique token.

    Handles the case where one source carries extra name parts the other drops:
    'James F. Condon School' vs 'Condon K-8 School' share only 'condon', so
    containment fails, yet they are plainly the same school.

    Safety comes from rarity within the geographic candidate set. 'condon'
    appears in exactly one nearby school, so it identifies. 'elementary'
    appears in dozens, so it doesn't -- which is what stops 'Holland
    Elementary' from matching 'Holmes Elementary', the highest-scoring wrong
    pair under character similarity (0.86). Grade span must still agree.
    """
    want = normalize(gs_name)
    if not want:
        return None

    freq = {}
    for c in candidates:
        for t in normalize(c.get(name_key, '')):
            freq[t] = freq.get(t, 0) + 1

    # Tokens that are rare locally AND long enough to be a real name, not an
    # initial or a grade fragment.
    distinctive = {t for t in want if freq.get(t, 0) == 1 and len(t) > 3}
    if not distinctive:
        return None

    scored = []
    for c in candidates:
        if gs_grades and not grades_compatible(gs_grades, c):
            continue
        shared = len(distinctive & normalize(c.get(name_key, '')))
        if shared:
            scored.append((shared, c))
    if not scored:
        return None
    # Several candidates can each own a different rare token ('james' here,
    # 'condon' there). Only accept when one shares strictly more than the rest.
    top = max(s for s, _ in scored)
    best = [c for s, c in scored if s == top]
    return best[0] if len(best) == 1 else None
