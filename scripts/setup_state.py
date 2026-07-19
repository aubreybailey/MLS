#!/usr/bin/env python3
"""
Build every school data layer for one state, in dependency order.

Reproducibility matters here because the layers were assembled interactively
and several steps have non-obvious failure modes that produced silently
incomplete data:

  * census.gov rate-limits bulk downloads with HTTP 429, which the old download
    script recorded as "this state has no such district type" -- that silently
    cost every Northeast SCSD file.
  * GreatSchools paginates at 25 results regardless of radius, so a single
    query in a dense area sees a fraction of what's there.
  * GreatSchools answers an empty search with HTTP 404, not an error.

Each step is idempotent and resumable: re-running skips work already done, so
a partial or interrupted run is fixed by running it again.

Steps
  1. boundaries   Census TIGER district shapefiles      -> data/tl_2023_*
  2. geopackage   merge into one indexed GeoPackage     -> data/school_districts.gpkg
  3. zones        NCES SABS attendance boundaries       -> data/attendance_zones.gpkg
  4. schools      NCES CCD school directory             -> cache/schools.db
  5. ratings      GreatSchools ratings per school       -> cache/schools.db

Usage
    python scripts/setup_state.py --state MA
    python scripts/setup_state.py --state MA --only ratings
    python scripts/setup_state.py --state MA --skip boundaries,zones
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

STEPS = ('boundaries', 'geopackage', 'zones', 'schools', 'ratings')


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
    return run(cmd, args.dry_run)


HANDLERS = {
    'boundaries': step_boundaries,
    'geopackage': step_geopackage,
    'zones': step_zones,
    'schools': step_schools,
    'ratings': step_ratings,
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
                         WHERE s.state = ? AND s.level = 'elementary'""", (state,))
            tot = q("SELECT COUNT(*) FROM schools WHERE state = ? AND level = 'elementary'", (state,))
            print(f"  elementary rated       {rated}/{tot} ({rated / tot * 100:.0f}%)" if tot else "")
            full = q("""SELECT COUNT(*) FROM (
                          SELECT s.leaid FROM schools s
                          LEFT JOIN school_ratings r ON r.ncessch = s.ncessch
                          WHERE s.state = ? AND s.level = 'elementary'
                          GROUP BY s.leaid
                          HAVING SUM(CASE WHEN r.rating IS NULL THEN 1 ELSE 0 END) = 0)""", (state,))
            dtot = q("SELECT COUNT(DISTINCT leaid) FROM schools WHERE state = ? AND level = 'elementary'", (state,))
            print(f"  districts fully rated  {full}/{dtot}   (these give a hard worst-case floor)")
    except Exception as e:
        print(f"  db check failed: {e}")

    print("\nDistricts without full ratings fall back to a partial floor, and "
          "districts\nwithout SABS zones report '*confirm elementary'. Both are "
          "expected.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
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

    print(f"Building school data for {state}: {' -> '.join(todo)}")
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
