#!/usr/bin/env python3
"""
Match MLS school labels to NCES school IDs.

Reads the harvested CSV (mls_school_labels.csv) and matches each school name
to an NCES ncessch ID using a multi-pass strategy:

  1. Conservative token-set matching (best_match)
  2. Distinctive-token matching (match_by_distinctive_token)
  3. Rule-based preprocessing: slash-split, compound-word split,
     abbreviation expansion, single-school-in-district
  4. Embedding similarity via an OpenAI-compatible API (embedding_match)

For each labeled point, candidates are the schools near the GPS coordinate,
scoped geographically so "Lincoln Elementary" in Springfield doesn't match
"Lincoln Elementary" in Boston.

Usage:
    python scripts/match_mls_to_nces.py
    python scripts/match_mls_to_nces.py --dry-run
    python scripts/match_mls_to_nces.py --no-embedding   # skip pass 4
    python scripts/match_mls_to_nces.py --embedding-url http://host:8082/v1/embeddings
"""

import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from school_match import best_match, match_by_distinctive_token

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(HERE, 'data', 'mls_school_labels.csv')
OUTPUT_CSV = os.path.join(HERE, 'data', 'mls_school_matched.csv')

FIELDNAMES = ['lat', 'lon', 'city', 'state', 'address', 'level',
              'mls_name', 'ncessch', 'nces_name', 'leaid', 'match_method']

VAGUE_LABELS = {
    '', 'n/a', 'none', 'other', 'unknown',
    'ranked choice', 'school choice', 'choice system', 'lottery',
    'choice', 'open enrollment', 'assigned',
    'per board of ed', 'per boe', 'pboe', 'school board',
    'local', 'apply', 'public/private', 'public & private',
    'see dept of ed', 'see supt', 'see remarks', 'tbd',
    'buyer to verify', 'contact listing agent', 'check with listing agent',
    'none selected', 'flex zone', 'priority enrollment',
}

ABBREV_MAP = {
    'st.': 'street', 'st': 'street', 'mt.': 'mount', 'mt': 'mount',
    'jfk': 'j f kennedy', 'mlk': 'martin luther king',
    'elem': 'elementary', 'elem.': 'elementary',
    'sch': 'school', 'sch.': 'school',
}


def _expand_abbreviations(name):
    """Expand common abbreviations in a school name."""
    words = name.split()
    out = []
    for w in words:
        wl = w.lower().rstrip('.,')
        if wl in ABBREV_MAP:
            out.append(ABBREV_MAP[wl])
        else:
            out.append(w)
    return ' '.join(out)


def _split_compound(name):
    """Split compound words: 'Meadowbrook' -> 'Meadow Brook' etc.

    Only splits if the result has each part >=4 chars (avoid 'The' + 'odore')."""
    words = name.split()
    result = []
    for w in words:
        if len(w) >= 8 and w[0].isupper():
            # Try splitting at each uppercase letter after the first
            parts = re.findall(r'[A-Z][a-z]+', w)
            if len(parts) >= 2 and all(len(p) >= 4 for p in parts):
                result.extend(parts)
                continue
        result.append(w)
    return ' '.join(result)


def _slash_variants(name):
    """Split 'Davis/Lane' into ['Davis', 'Lane'] for independent matching."""
    if '/' not in name:
        return []
    parts = [p.strip() for p in name.split('/')]
    return [p for p in parts if len(p) >= 3 and p.lower() not in VAGUE_LABELS]


def _try_base_matchers(name, cands):
    """Try best_match + distinctive_token on a name variant."""
    hit = best_match(name, cands)
    if hit:
        return hit
    return match_by_distinctive_token(name, cands)


def _match_school(name, lat, lon, level):
    """Multi-pass matching: rule-based first, returns (hit, method, candidates)."""
    # Collect candidates at widest radius we'll need (reuse for all passes)
    all_cands = {}
    for radius in (4.0, 8.0):
        cands = db.schools_near(lat, lon, radius, level, 100)
        if cands:
            all_cands[radius] = cands

    # Pass 1-2: conservative matchers at each radius
    for radius, cands in all_cands.items():
        hit = best_match(name, cands)
        if hit:
            return hit, 'best_match', cands
        hit = match_by_distinctive_token(name, cands)
        if hit:
            return hit, 'distinctive_token', cands

    # Use widest candidate set for remaining passes
    cands = all_cands.get(8.0, all_cands.get(4.0, []))
    if not cands:
        return None, None, []

    # Pass 3a: expand abbreviations and retry
    expanded = _expand_abbreviations(name)
    if expanded != name:
        hit = _try_base_matchers(expanded, cands)
        if hit:
            return hit, 'abbrev_expand', cands

    # Pass 3b: split compound words ('Meadowbrook' -> 'Meadow Brook')
    split = _split_compound(name)
    if split != name:
        hit = _try_base_matchers(split, cands)
        if hit:
            return hit, 'compound_split', cands

    # Pass 3c: slash-separated names — try each part independently
    parts = _slash_variants(name)
    if parts:
        for part in parts:
            hit = _try_base_matchers(part, cands)
            if hit:
                return hit, 'slash_split', cands
        # Also try with abbreviation expansion on each part
        for part in parts:
            exp = _expand_abbreviations(part)
            if exp != part:
                hit = _try_base_matchers(exp, cands)
                if hit:
                    return hit, 'slash_expand', cands

    # Pass 3d: single-school-in-district — if there's only one candidate,
    # and the name isn't obviously junk, it's the only possible match
    if len(cands) == 1 and len(name) >= 4:
        nl = name.lower()
        if nl not in VAGUE_LABELS and not any(w in nl for w in
                ('check', 'see ', 'contact', 'verify', 'buyer', 'tbd',
                 'board', 'supt', 'per ', 'apply')):
            return cands[0], 'single_school', cands

    return None, None, cands


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--level', default='elementary',
                    help='which level to match (default: elementary)')
    ap.add_argument('--all-levels', action='store_true',
                    help='match all levels (elementary, middle, high)')
    ap.add_argument('--no-embedding', action='store_true',
                    help='skip embedding-based matching (pass 4)')
    ap.add_argument('--embedding-url',
                    help='OpenAI-compatible embeddings endpoint URL')
    ap.add_argument('--embedding-threshold', type=float, default=0.76,
                    help='cosine similarity threshold (default 0.76)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(INPUT_CSV):
        print(f"No input file at {INPUT_CSV}")
        print("Run harvest_mls_zones.py first.")
        return 1

    embed_fn = None
    if not args.no_embedding:
        try:
            from embedding_match import embedding_match
            embed_fn = embedding_match
            endpoint = args.embedding_url or os.environ.get('EMBEDDING_URL', 'default')
            print(f"Embedding matching enabled (endpoint: {endpoint})")
        except ImportError:
            print("embedding_match not available, skipping pass 4")

    levels = ['elementary', 'middle', 'high'] if args.all_levels else [args.level]

    with open(INPUT_CSV, newline='') as f:
        rows = list(csv.DictReader(f))
    print(f"Read {len(rows)} labeled points from {INPUT_CSV}")

    matched = 0
    unmatched = 0
    embed_pending = []
    output_rows = []

    for level in levels:
        level_key = level
        points_with_label = [(r, r[level_key]) for r in rows if r.get(level_key)]
        # Filter out vague labels that slipped through the harvester
        points_with_label = [(r, n) for r, n in points_with_label
                             if n.strip().lower() not in VAGUE_LABELS]
        print(f"\n{level}: {len(points_with_label)} points with labels")

        for row, mls_name in points_with_label:
            lat = float(row['lat'])
            lon = float(row['lon'])

            hit, method, cands = _match_school(mls_name, lat, lon, level)

            out = {
                'lat': row['lat'],
                'lon': row['lon'],
                'city': row['city'],
                'state': row['state'],
                'address': row['address'],
                'level': level,
                'mls_name': mls_name,
                'ncessch': hit['ncessch'] if hit else '',
                'nces_name': hit['name'] if hit else '',
                'leaid': hit['leaid'] if hit else '',
                'match_method': method or '',
            }
            output_rows.append(out)

            if hit:
                matched += 1
            else:
                unmatched += 1
                if embed_fn and cands:
                    embed_pending.append((len(output_rows) - 1, mls_name, cands))

    rule_matched = matched
    total = matched + unmatched
    print(f"\nRule-based: {matched} matched, {unmatched} unmatched "
          f"({matched/total*100:.0f}%)")

    # Pass 4: embedding fallback
    if embed_pending and embed_fn:
        print(f"\nPass 4: embedding matching on {len(embed_pending)} unmatched names...")
        embed_matched = 0
        endpoint = args.embedding_url
        threshold = args.embedding_threshold

        for i, (idx, name, cands) in enumerate(embed_pending):
            hit = embed_fn(name, cands, endpoint=endpoint, threshold=threshold)
            if hit:
                output_rows[idx]['ncessch'] = hit['ncessch']
                output_rows[idx]['nces_name'] = hit['name']
                output_rows[idx]['leaid'] = hit['leaid']
                output_rows[idx]['match_method'] = 'embedding'
                embed_matched += 1
            if (i + 1) % 200 == 0:
                print(f"  ...{i+1}/{len(embed_pending)} "
                      f"({embed_matched} new matches)")

        matched += embed_matched
        unmatched -= embed_matched
        print(f"  Embedding pass: {embed_matched} new matches")
        print(f"\nTotal: {matched} matched, {unmatched} unmatched "
              f"({matched/(matched+unmatched)*100:.0f}%)")

    if args.dry_run:
        print("\nDry run — not writing output.")
        _print_summary(output_rows, matched, embed_fn is not None)
        return 0

    with open(OUTPUT_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Output: {OUTPUT_CSV}")
    _print_summary(output_rows, matched, embed_fn is not None)
    return 0


def _print_summary(output_rows, matched, has_embedding):
    from collections import Counter
    methods = Counter(r['match_method'] for r in output_rows if r['match_method'])
    print(f"\nMatch methods: {dict(methods)}")

    unmatched_names = Counter(r['mls_name'] for r in output_rows if not r['ncessch'])
    if unmatched_names:
        print(f"\nTop unmatched names ({len(unmatched_names)} unique):")
        for name, count in unmatched_names.most_common(20):
            print(f"  {name:<40} x{count}")


if __name__ == '__main__':
    sys.exit(main())
