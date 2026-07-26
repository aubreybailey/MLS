#!/usr/bin/env python3
"""
GreatSchools Rating Scraper

Fetches school ratings from GreatSchools.org search results.
"""

import json
import re
import time
import requests
from dataclasses import dataclass
from typing import Optional

REQUEST_TIMEOUT = 15

# GreatSchools returns 25 results per page regardless of radius.
PAGE_SIZE = 25
MAX_PAGES = 8

# Widening steps used when a search returns no schools at all (see below).
RADIUS_ESCALATION = [3, 10, 25]


@dataclass
class SchoolRating:
    name: str
    rating: Optional[int]
    rating_scale: str
    city: str
    state: str
    grades: str
    school_type: str
    profile_url: str


def search_schools_by_location(
    lat: float,
    lon: float,
    radius_miles: int = 5,
    grade_levels: str = 'e',
    max_pages: int = MAX_PAGES,
) -> list[SchoolRating]:
    """
    Search GreatSchools by location and extract school ratings.

    Results are paginated at 25 per page regardless of radius, so a single
    request in a dense area sees only a fraction of what's there -- Boston has
    109 schools and one query returns 25 of them. Widening the radius does not
    help (distance=2,3,5,10 all return exactly 25); only paging does.
    """
    return _search_paged(lat, lon, radius_miles, grade_levels, max_pages)


def _unescape(text: str) -> str:
    """Decode JSON escapes left in the scraped strings.

    Names are pulled straight out of an embedded JSON blob with a regex, so
    sequences like \\u0026 survive as literal text -- 'Goddard School of
    Science \\u0026 Technology'. That breaks name matching against NCES.
    """
    try:
        return json.loads(f'"{text}"')
    except Exception:
        return text


def _search_page(lat, lon, radius_miles, grade_levels, page):
    url = "https://www.greatschools.org/search/search.page"
    params = {
        "lat": lat,
        "lon": lon,
        "distance": radius_miles,
        "gradeLevels": grade_levels,
    }
    if page > 1:
        params["page"] = page
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        # GreatSchools answers an empty search with HTTP 404 (the body reads
        # "No results"), not an error. In rural areas nothing is indexed within
        # the default radius, so widen rather than give up -- otherwise those
        # districts never get ratings and fall through to the unreliable
        # area-average path. Not a bot wall: no captcha is involved.
        if (resp.status_code == 404 and page == 1
                and radius_miles < RADIUS_ESCALATION[-1]):
            for wider in [r for r in RADIUS_ESCALATION if r > radius_miles]:
                params['distance'] = wider
                resp = requests.get(url, params=params, headers=headers,
                                    timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    break
        resp.raise_for_status()
        html = resp.text

        schools = []
        seen_names = set()

        schools_array_match = re.search(r'"schools":\s*\[', html)

        if schools_array_match:
            start_pos = schools_array_match.end()
            pos = start_pos
            brace_count = 0
            obj_start = None

            while pos < len(html):
                ch = html[pos]
                if ch == '{':
                    if brace_count == 0:
                        obj_start = pos
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0 and obj_start is not None:
                        obj_str = html[obj_start:pos+1]

                        name_m = re.search(r'"name":"([^"]+)"', obj_str)
                        rating_m = re.search(r'"rating":(\d+)', obj_str)
                        grades_m = re.search(r'"gradeLevels":"([^"]+)"', obj_str)
                        city_m = re.search(r'"city":"([^"]+)"', obj_str)
                        state_m = re.search(r'"state":"([^"]+)"', obj_str)
                        school_type_m = re.search(r'"schoolType":"([^"]+)"', obj_str)
                        profile_m = re.search(r'"profile":"([^"]+)"', obj_str)

                        if name_m:
                            name = _unescape(name_m.group(1))
                            if name not in seen_names:
                                seen_names.add(name)
                                schools.append(SchoolRating(
                                    name=name,
                                    rating=int(rating_m.group(1)) if rating_m else None,
                                    rating_scale='',
                                    city=_unescape(city_m.group(1)) if city_m else '',
                                    state=state_m.group(1) if state_m else '',
                                    grades=_unescape(grades_m.group(1)) if grades_m else '',
                                    school_type=school_type_m.group(1) if school_type_m else '',
                                    profile_url=f"https://www.greatschools.org{profile_m.group(1)}" if profile_m else ''
                                ))

                        obj_start = None
                elif ch == ']' and brace_count == 0:
                    break
                pos += 1

        return schools

    except Exception as e:
        print(f"Error searching GreatSchools: {e}")
        return []


def _search_paged(lat, lon, radius_miles, grade_levels, max_pages):
    """Walk pages until one comes back short, empty, or we hit max_pages.

    A short page means we've reached the end of the result set; a 404 on page
    >1 means the same. Dedup by name across pages since the API can repeat
    entries near page boundaries.
    """
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        batch = _search_page(lat, lon, radius_miles, grade_levels, page)
        if not batch:
            break
        new = [s for s in batch if s.name not in seen]
        seen.update(s.name for s in batch)
        out.extend(new)
        if len(batch) < PAGE_SIZE:
            break
    return out


def classify_school_level(grades: str) -> str:
    """Classify a school into elementary/middle/high/other based on grade range."""
    if not grades:
        return 'other'

    grades_lower = grades.lower()
    nums = re.findall(r'\d+', grades)
    has_k = 'pk' in grades_lower or ('k' in grades_lower and 'kg' not in grades_lower)

    if not nums:
        if has_k:
            return 'elementary'
        return 'other'

    nums = [int(n) for n in nums]
    low = min(nums)
    high = max(nums)

    if has_k:
        low = 0

    if low <= 2 and high <= 6:
        return 'elementary'
    elif low >= 5 and high <= 9 and high >= 7:
        return 'middle'
    elif low >= 9 and high <= 12:
        return 'high'
    elif low <= 2 and high >= 8:
        return 'other'
    elif low >= 5 and high >= 12:
        return 'other'
    else:
        return 'other'


def get_ratings_by_level(lat: float, lon: float, radius: int = 3) -> dict:
    """
    Get school ratings broken down by level (elementary, middle, high, other).
    """
    schools = search_schools_by_location(lat, lon, radius, 'e,m,h')

    by_level = {
        'elementary': [],
        'middle': [],
        'high': [],
        'other': []
    }

    for school in schools:
        level = classify_school_level(school.grades)
        by_level[level].append(school)

    result = {}
    for level, level_schools in by_level.items():
        rated = [s for s in level_schools if s.rating is not None]
        if rated:
            avg = round(sum(s.rating for s in rated) / len(rated), 1)
            top = max(rated, key=lambda s: s.rating)
            result[level] = {
                'rating': avg,
                'count': len(rated),
                'top_school': top.name,
                'top_rating': top.rating,
                # Keep every rated school, not just the first few: the address's
                # assigned school (from SABS attendance zones) is often well
                # down this list, and truncating meant we could never attach a
                # rating to it. Dense areas return ~20 per level, which is small.
                'schools': [{'name': s.name, 'rating': s.rating, 'grades': s.grades} for s in rated]
            }
        else:
            result[level] = {
                'rating': None,
                'count': 0,
                'top_school': None,
                'top_rating': None,
                'schools': []
            }

    return result


# ---------------------------------------------------------------------------
# City-table enumeration
#
# Borrowed from the CitySpire project's approach (labspt15-cityspire-g-ds),
# with two upgrades: no selenium (plain requests works against this endpoint
# now), and the embedded "schools" JSON instead of scraping the HTML table.
#
# Why this exists alongside the lat/lon radius search: the city table is a
# COMPLETE enumeration with a stated total ("Showing 1 to 17 of 17"), so
# coverage is verifiable per city -- a radius sweep can never prove it saw
# everything. Each row also carries lat/lon, street address and districtName,
# which are far stronger matching signals than a bare name.

STATE_NAMES = {
    'AL': 'alabama', 'AK': 'alaska', 'AZ': 'arizona', 'AR': 'arkansas',
    'CA': 'california', 'CO': 'colorado', 'CT': 'connecticut',
    'DE': 'delaware', 'DC': 'district-of-columbia', 'FL': 'florida',
    'GA': 'georgia', 'HI': 'hawaii', 'ID': 'idaho', 'IL': 'illinois',
    'IN': 'indiana', 'IA': 'iowa', 'KS': 'kansas', 'KY': 'kentucky',
    'LA': 'louisiana', 'ME': 'maine', 'MD': 'maryland',
    'MA': 'massachusetts', 'MI': 'michigan', 'MN': 'minnesota',
    'MS': 'mississippi', 'MO': 'missouri', 'MT': 'montana',
    'NE': 'nebraska', 'NV': 'nevada', 'NH': 'new-hampshire',
    'NJ': 'new-jersey', 'NM': 'new-mexico', 'NY': 'new-york',
    'NC': 'north-carolina', 'ND': 'north-dakota', 'OH': 'ohio',
    'OK': 'oklahoma', 'OR': 'oregon', 'PA': 'pennsylvania',
    'RI': 'rhode-island', 'SC': 'south-carolina', 'SD': 'south-dakota',
    'TN': 'tennessee', 'TX': 'texas', 'UT': 'utah', 'VT': 'vermont',
    'VA': 'virginia', 'WA': 'washington', 'WV': 'west-virginia',
    'WI': 'wisconsin', 'WY': 'wyoming',
}


def _parse_school_objects(html: str) -> list[dict]:
    """Full school dicts from the embedded "schools" JSON array."""
    m = re.search(r'"schools":\s*\[', html)
    if not m:
        return []
    out, depth, start = [], 0, None
    for j in range(m.end(), len(html)):
        c = html[j]
        if c == '{':
            if depth == 0:
                start = j
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    out.append(json.loads(html[start:j + 1]))
                except Exception:
                    pass
        elif c == ']' and depth == 0:
            break
    return out


def get_city_schools(state_abbr: str, city: str, verbose: bool = False) -> dict:
    """Every school GreatSchools lists for one city, with a verifiable total.

    Returns {'total': int|None, 'schools': [...]} where each school keeps the
    fields that matter for matching: name, rating, gradeLevels, levelCode,
    lat, lon, address, districtName, schoolType, enrollment.
    total is what the page claims exists; len(schools) < total means the sweep
    is knowingly incomplete (unlike the radius search, which cannot know).
    """
    state = STATE_NAMES.get(state_abbr.upper())
    if not state:
        return {'total': None, 'schools': []}
    slug = city.strip().lower().replace(' ', '-')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
    }

    schools, seen, total = [], set(), None
    for page in range(1, 40):
        url = (f'https://www.greatschools.org/{state}/{slug}/schools/'
               f'?page={page}&tableView=Overview&view=table')
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                                allow_redirects=True)
            if resp.status_code != 200:
                break
            html = resp.text
        except Exception:
            break

        m = re.search(r'[Ss]howing\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)', html)
        batch = _parse_school_objects(html)
        new = 0
        for d in batch:
            key = (d.get('name'), (d.get('address') or {}).get('street1'))
            if key in seen:
                continue
            seen.add(key)
            schools.append({
                'name': _unescape(str(d.get('name') or '')),
                'rating': d.get('rating'),
                'grades': d.get('gradeLevels') or '',
                'level_code': d.get('levelCode') or '',
                'lat': d.get('lat'), 'lon': d.get('lon'),
                'street': ((d.get('address') or {}).get('street1')) or '',
                'district_name': _unescape(str(d.get('districtName') or '')),
                'school_type': d.get('schoolType') or '',
                'enrollment': d.get('enrollment'),
            })
            new += 1
        if verbose:
            print(f'    page {page}: +{new}')
        if m:
            total = int(m.group(3))
            if int(m.group(2)) >= total:
                break
        if not batch or new == 0:
            break
        time.sleep(0.4)

    return {'total': total, 'schools': schools}


def parse_assigned_school(page_text: str):
    """Extract the assigned school from GreatSchools' "Schools by Address" page.

    The page (rendered by a real browser -- curl gets no assignment) lists
    schools by distance; the one the address is *assigned* to has a line
    "Assigned school in <district>" right after its block. Each school block is
    "<name>\\n<rating>\\n/10\\nGreatSchools Rating ...", so the assigned school is
    the last such block before the marker -- correct regardless of sort order.

    Returns (school_name, district) or (None, None). Deterministic: no judgment
    for a caller (or a small model) to get wrong.
    """
    m = re.search(r'Assigned school in ([^\n]+)', page_text)
    if not m:
        return None, None
    district = m.group(1).strip()
    before = page_text[:m.start()]
    entries = re.findall(r'([^\n]+?)\n\d+\n/10\nGreatSchools Rating', before)
    if not entries:
        return None, district
    return entries[-1].strip(), district
