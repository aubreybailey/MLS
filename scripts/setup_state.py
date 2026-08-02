#!/usr/bin/env python3
"""
Build every school data layer for one state, in dependency order.

By default, the pipeline uses only freely available government data sources
(Census TIGER boundaries, NCES school directory, NCES SABS attendance zones,
and state test proficiency data).  Pass --non-free to also run the
GreatSchools rating scrape and zone-sample collection, which produce better
coverage but cannot be redistributed.

Each step is idempotent and resumable: re-running skips work already done, so
a partial or interrupted run is fixed by running it again.

Steps (default / free pipeline)
  1. boundaries   Census TIGER district shapefiles      -> data/tl_2023_*
  2. geopackage   merge into one indexed GeoPackage     -> data/school_districts.gpkg
  3. zones        NCES SABS attendance boundaries       -> data/attendance_zones.gpkg
  4. schools      NCES CCD school directory             -> cache/schools.db
  5. mcas         state test proficiency -> rating       -> cache/schools.db
                  (also tags PK/K-2 and alt programs)
  6. classify     district zoning style classification  -> cache/schools.db

Additional steps with --non-free
  5b. ratings     GreatSchools ratings per school       -> cache/schools.db
  5c. samples     load committed zone-sample seed       -> cache/schools.db

Usage
    python scripts/setup_state.py --state MA
    python scripts/setup_state.py --state MA --non-free
    python scripts/setup_state.py --state MA --only ratings
    python scripts/setup_state.py --state MA --dry-run
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, 'data')

STEPS = ('boundaries', 'geopackage', 'zones', 'schools', 'mcas',
         'ratings', 'samples', 'classify')

NON_FREE_STEPS = {'ratings', 'samples'}


def run(cmd, dry_run=False) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    if dry_run:
        return 0
    return subprocess.call(cmd)


def step_boundaries(state, args):
    """TIGER district shapefiles. Downloads all states -- TIGER is packaged per
    state but the download script covers the country in one pass, and it is
    cheap to skip files already present."""
    script = os.path.join(ROOT, 'download_data.sh')
    if not os.path.exists(script):
        print(f"missing {script}")
        return 1
    return run(['bash', script], args.dry_run)


def step_geopackage(state, args):
    """Merge the shapefiles into one spatially indexed GeoPackage."""
    cmd = [sys.executable, os.path.join(HERE, 'build_geopackage.py')]
    if args.force:
        cmd.append('--force')
    return run(cmd, args.dry_run)


def step_zones(state, args):
    """NCES SABS attendance boundaries (~557MB national download).

    Coverage is partial by design -- participation was voluntary -- so a state
    with few or no zones is a legitimate outcome, not a failure."""
    cmd = [sys.executable, os.path.join(HERE, 'build_attendance_zones.py'),
           '--state', state]
    if args.sabs_zip:
        cmd += ['--sabs-zip', args.sabs_zip]
    if args.force:
        cmd.append('--force')
    return run(cmd, args.dry_run)


def step_schools(state, args):
    """NCES CCD school directory -> the schools table."""
    cmd = [sys.executable, os.path.join(HERE, 'build_schools_table.py'),
           '--state', state, '--year', str(args.year)]
    return run(cmd, args.dry_run)


def step_ratings(state, args):
    """GreatSchools ratings, one row per school. Resumable: re-running only
    fetches schools that still have no rating."""
    cmd = [sys.executable, os.path.join(HERE, 'backfill_school_ratings.py'),
           '--state', state, '--delay', str(args.delay)]
    if args.level:
        cmd += ['--level', args.level]
    rc = run(cmd, args.dry_run)
    if not args.dry_run:
        _tag_pk_schools(state)
        _tag_alt_schools(state)
    return rc


def _tag_pk_schools(state):
    """Tag schools whose grade range is below state-testing grades (PK/K-2) as
    'not-rated-pk'. GreatSchools structurally cannot rate these, so the absence
    is explained — not a gap."""
    sys.path.insert(0, ROOT)
    import db
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    unrated_pk = conn.execute(
        'SELECT s.ncessch, s.grade_hi FROM schools s '
        'LEFT JOIN school_ratings r ON s.ncessch = r.ncessch '
        'WHERE s.state = ? AND s.level = "elementary" AND s.grade_hi < 3 '
        'AND r.ncessch IS NULL', (state,)).fetchall()
    if not unrated_pk:
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for nc, ghi in unrated_pk:
        db.put_school_rating(nc, None, f'grades end at {ghi}, below testing',
                             source='not-rated-pk')
    print(f"  tagged {len(unrated_pk)} PK/K-2 schools as not-rated-pk")


_ALT_KEYWORDS = [
    'therapeutic', 'alternative', 'adult tech',
    'gateway to college', 'opportunity academy',
    'virtual', 'academy for success',
    'cooperative alternative', 'day school',
    'transition academy', 'transitions academy',
]


def _tag_alt_schools(state):
    """Tag alt/therapeutic/virtual/transition programs as 'not-rated-alt'.
    These aren't geographically assigned neighborhood schools — a house is
    never automatically zoned into an adult academy or a therapeutic day
    program, so they shouldn't be the floor in a district-min calculation."""
    sys.path.insert(0, ROOT)
    import db
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    unrated = conn.execute(
        'SELECT s.ncessch, s.name, s.grade_hi, s.enrollment FROM schools s '
        'LEFT JOIN school_ratings r ON s.ncessch = r.ncessch '
        'WHERE s.state = ? AND r.ncessch IS NULL', (state,)).fetchall()
    tagged = 0
    for nc, name, ghi, enroll in unrated:
        nl = name.lower()
        if ghi == 15:
            reason = 'post-secondary transition (grade 15)'
        elif any(kw in nl for kw in _ALT_KEYWORDS):
            reason = next(kw for kw in _ALT_KEYWORDS if kw in nl)
        elif enroll is not None and enroll <= 30 and \
                any(h in nl for h in ('academy', 'prep')):
            reason = f'tiny alt ({enroll} students)'
        else:
            continue
        db.put_school_rating(nc, None, reason, source='not-rated-alt')
        tagged += 1
    if tagged:
        print(f"  tagged {tagged} alt/therapeutic/virtual schools as not-rated-alt")


def step_mcas(state, args):
    """MCAS state test proficiency, converted to a 1-10 rating.

    In the default (free) pipeline, MCAS is the primary rating source and
    also runs PK/alt tagging afterward.  With --non-free, GreatSchools runs
    first and MCAS gap-fills only unrated schools (PK/alt tagging is handled
    by the ratings step instead)."""
    if state != 'MA':
        print(f"  MCAS is Massachusetts-only (skipping {state})")
        return 0
    cmd = [sys.executable, os.path.join(HERE, 'backfill_mcas.py'),
           '--state', state]
    if not args.non_free:
        cmd.append('--primary')
    if args.dry_run:
        cmd.append('--dry-run')
    rc = run(cmd, args.dry_run)
    if not args.non_free and not args.dry_run:
        _tag_pk_schools(state)
        _tag_alt_schools(state)
    return rc


def step_samples(state, args):
    """Load committed zone-sample seed into the zone_samples table."""
    csv_path = os.path.join(HERE, 'data', 'zone_samples.csv')
    if not os.path.exists(csv_path):
        print(f"  no zone samples seed at {csv_path} (optional)")
        return 0
    return run([sys.executable, os.path.join(HERE, 'load_zone_samples.py')],
               args.dry_run)


def step_classify(state, args):
    """Classify district zoning style (zoned/choice/single/unknown)."""
    return run([sys.executable, os.path.join(HERE, 'classify_districts.py'),
                '--state', state], args.dry_run)


HANDLERS = {
    'boundaries': step_boundaries,
    'geopackage': step_geopackage,
    'zones': step_zones,
    'mcas': step_mcas,
    'schools': step_schools,
    'ratings': step_ratings,
    'samples': step_samples,
    'classify': step_classify,
}


def verify(state):
    """Report what actually landed, so a partial build is visible."""
    print("\n" + "=" * 62)
    print(f"VERIFICATION - {state}")
    print("=" * 62)

    gpkg = os.path.join(DATA, 'school_districts.gpkg')
    zones = os.path.join(DATA, 'attendance_zones.gpkg')
    for label, path in (('district GeoPackage', gpkg), ('attendance zones', zones)):
        if os.path.exists(path):
            print(f"  {label:<22} {os.path.getsize(path) / 1048576:.0f}MB")
        else:
            print(f"  {label:<22} MISSING")

    try:
        sys.path.insert(0, ROOT)
        import db
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        q = lambda s, a=(): conn.execute(s, a).fetchone()[0]
        n = q('SELECT COUNT(*) FROM schools WHERE state = ?', (state,))
        print(f"  schools                {n}")
        if n:
            rated = q("""SELECT COUNT(*) FROM schools s JOIN school_ratings r
                         ON r.ncessch = s.ncessch
                         WHERE s.state = ? AND s.level = 'elementary'
                         AND r.rating IS NOT NULL""", (state,))
            pk = q("""SELECT COUNT(*) FROM schools s JOIN school_ratings r
                      ON r.ncessch = s.ncessch
                      WHERE s.state = ? AND s.level = 'elementary'
                      AND r.source = 'not-rated-pk'""", (state,))
            tot = q("SELECT COUNT(*) FROM schools WHERE state = ? AND level = 'elementary'", (state,))
            alt_elem = q("""SELECT COUNT(*) FROM schools s JOIN school_ratings r
                         ON r.ncessch = s.ncessch
                         WHERE s.state = ? AND s.level = 'elementary'
                         AND r.source = 'not-rated-alt'""", (state,))
            alt_all = q("""SELECT COUNT(*) FROM school_ratings
                       WHERE source = 'not-rated-alt'
                       AND ncessch IN (SELECT ncessch FROM schools WHERE state = ?)""", (state,))
            gap = tot - rated - pk - alt_elem
            print(f"  elementary rated       {rated}/{tot} ({rated / tot * 100:.0f}%)" if tot else "")
            if pk:
                print(f"  PK/K-2 (no testing)   {pk}  (tagged, not a gap)")
            if alt_all:
                print(f"  alt/virtual/etc       {alt_all}  (tagged, not neighborhood schools)")
            if gap:
                print(f"  truly unrated         {gap}")
            full = q("""SELECT COUNT(*) FROM (
                          SELECT s.leaid FROM schools s
                          LEFT JOIN school_ratings r ON r.ncessch = s.ncessch
                          WHERE s.state = ? AND s.level = 'elementary'
                          AND COALESCE(r.source, '') NOT IN ('not-rated-alt')
                          GROUP BY s.leaid
                          HAVING SUM(CASE WHEN r.rating IS NULL THEN 1 ELSE 0 END) = 0)""", (state,))
            dtot = q("SELECT COUNT(DISTINCT leaid) FROM schools WHERE state = ? AND level = 'elementary'", (state,))
            print(f"  districts fully rated  {full}/{dtot}   (these give a hard worst-case floor)")
            # Provenance summary
            sources = conn.execute(
                'SELECT r.source, COUNT(*) FROM school_ratings r '
                'JOIN schools s ON s.ncessch = r.ncessch '
                'WHERE s.state = ? GROUP BY r.source ORDER BY COUNT(*) DESC',
                (state,)).fetchall()
            gov = sum(c for s, c in sources
                      if db.provenance_of(s) in ('government', 'inferred'))
            scraped = sum(c for s, c in sources
                         if db.provenance_of(s) == 'scraped')
            print(f"\n  Provenance:")
            for src, cnt in sources:
                prov = db.provenance_of(src)
                print(f"    {src:<25} {cnt:>5}  ({prov})")
            if scraped:
                print(f"\n  {scraped} ratings from scraped sources "
                      f"(run without --non-free for a clean build)")
            else:
                print(f"\n  All {gov} ratings trace to government sources")
    except Exception as e:
        print(f"  db check failed: {e}")

    print("\nDistricts without full ratings fall back to a partial floor, and "
          "districts\nwithout SABS zones report '*confirm elementary'. Both are "
          "expected.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--non-free', action='store_true',
                    help='also run GreatSchools scrape and zone-sample steps')
    ap.add_argument('--only', help=f"comma-separated subset of: {', '.join(STEPS)}")
    ap.add_argument('--skip', help='comma-separated steps to skip')
    ap.add_argument('--sabs-zip', help='path to an already-downloaded SABS_1516.zip')
    ap.add_argument('--year', type=int, default=2022, help='CCD year (default 2022)')
    ap.add_argument('--delay', type=float, default=0.6, help='GreatSchools request delay')
    ap.add_argument('--level', default='elementary',
                    help="rating backfill level; '' for all levels")
    ap.add_argument('--force', action='store_true', help='rebuild derived files')
    ap.add_argument('--dry-run', action='store_true', help='print commands only')
    args = ap.parse_args()

    state = args.state.upper()
    todo = list(STEPS)
    if not args.non_free:
        todo = [s for s in todo if s not in NON_FREE_STEPS]
    if args.only:
        want = [s.strip() for s in args.only.split(',')]
        bad = [s for s in want if s not in STEPS]
        if bad:
            print(f"unknown step(s): {bad}. valid: {', '.join(STEPS)}")
            return 1
        todo = [s for s in todo if s in want]
    if args.skip:
        skip = {s.strip() for s in args.skip.split(',')}
        todo = [s for s in todo if s not in skip]

    mode = 'non-free (GreatSchools + MCAS)' if args.non_free else 'free (government data only)'
    print(f"Building school data for {state} [{mode}]: {' -> '.join(todo)}")
    started = time.time()
    failed = []
    for name in todo:
        print(f"\n{'=' * 62}\nSTEP: {name}\n{'=' * 62}")
        rc = HANDLERS[name](state, args)
        if rc != 0:
            failed.append(name)
            print(f"  step '{name}' returned {rc}; continuing so later steps "
                  f"still run (re-run to retry).")

    print(f"\nfinished in {time.time() - started:.0f}s")
    if failed:
        print(f"steps that reported failure: {', '.join(failed)} -- re-run to retry")
    if not args.dry_run:
        verify(state)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
