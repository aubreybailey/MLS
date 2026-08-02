#!/usr/bin/env python3
"""
Bayesian school zone classifier.

Combines a Voronoi distance prior (closer to school → higher prior) with
evidence from nearby labeled MLS/GS sample points. When data is sparse,
behaves like Voronoi; when data is dense, the samples override geometry.

The posterior for each candidate school at a query point (lat, lon):

    P(school | location, samples) ∝ P(location | school) × P(school | samples)

  Prior (Voronoi):
    P(location | school_i) = exp(-d_i / σ) / Σ exp(-d_j / σ)
    where d_i is distance in miles to school_i, σ is a temperature parameter

  Likelihood (sample evidence):
    P(school | samples) = (n_i + α) / (N + α × K)
    where n_i = weighted vote count for school_i from nearby samples,
    N = total weighted votes, K = number of candidate schools,
    α = pseudocount (Laplace smoothing)

  Weighting: each sample's vote is weighted by exp(-d_sample / λ)
    where d_sample is distance from query point to sample, λ = decay scale

Usage as module:
    from bayesian_zone import classify_point
    result = classify_point(lat, lon, level='elementary', db_conn=conn)
    # result = {'ncessch': ..., 'school_name': ..., 'posterior': ...,
    #           'prior': ..., 'evidence': ..., 'n_samples': ...}

Usage as script (batch classification):
    python scripts/bayesian_zone.py --state MA --level elementary
"""

import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Voronoi prior temperature: larger σ = flatter prior (less confident in geometry)
SIGMA_MILES = 1.5

# Sample evidence decay: larger λ = samples influence from farther away
LAMBDA_MILES = 0.4

# Laplace smoothing pseudocount
ALPHA = 0.5

# Maximum radius to search for samples
SAMPLE_RADIUS_MILES = 1.0


def _haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _voronoi_prior(lat, lon, schools, sigma=SIGMA_MILES):
    """Softmax prior over schools based on distance.

    Returns dict {ncessch: probability}."""
    dists = {}
    for nc, slat, slon, *_ in schools:
        d = _haversine(lat, lon, slat, slon)
        dists[nc] = d

    log_scores = {nc: -d / sigma for nc, d in dists.items()}
    max_log = max(log_scores.values())
    exp_scores = {nc: math.exp(s - max_log) for nc, s in log_scores.items()}
    total = sum(exp_scores.values())

    return {nc: s / total for nc, s in exp_scores.items()}


def _sample_evidence(lat, lon, level, conn, candidate_ncs,
                     radius=SAMPLE_RADIUS_MILES, lam=LAMBDA_MILES,
                     alpha=ALPHA):
    """Weighted vote from nearby labeled samples.

    Returns (dict {ncessch: posterior}, n_samples_used)."""
    dlat = radius / 69.0
    dlon = radius / max(0.1, 69.0 * math.cos(math.radians(lat)))

    rows = conn.execute(
        'SELECT lat, lon, ncessch FROM zone_samples '
        'WHERE ncessch IS NOT NULL AND level = ? '
        'AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?',
        (level, lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()

    votes = {nc: 0.0 for nc in candidate_ncs}
    n_used = 0

    for slat, slon, snc in rows:
        if snc not in candidate_ncs:
            continue
        d = _haversine(lat, lon, slat, slon)
        if d > radius:
            continue
        w = math.exp(-d / lam)
        votes[snc] = votes.get(snc, 0.0) + w
        n_used += 1

    K = len(candidate_ncs)
    total = sum(votes.values()) + alpha * K
    evidence = {nc: (votes.get(nc, 0.0) + alpha) / total for nc in candidate_ncs}

    return evidence, n_used


def classify_point(lat, lon, schools, level, conn,
                   sigma=SIGMA_MILES, lam=LAMBDA_MILES,
                   alpha=ALPHA, radius=SAMPLE_RADIUS_MILES):
    """Classify a point to a school using Bayesian combination.

    schools: list of (ncessch, lat, lon, enrollment) tuples
    Returns dict with 'ncessch', 'school_name', 'posterior', 'prior',
    'evidence', 'n_samples', or None if no schools."""
    if not schools:
        return None

    candidate_ncs = {s[0] for s in schools}
    school_names = {s[0]: s[1] if len(s) > 4 else s[0] for s in schools}
    # Handle both (nc, lat, lon, enrollment) and (nc, name, lat, lon, enrollment)
    school_locs = []
    for s in schools:
        if len(s) == 5:
            school_locs.append((s[0], s[2], s[3], s[4]))
            school_names[s[0]] = s[1]
        else:
            school_locs.append(s)

    prior = _voronoi_prior(lat, lon, school_locs, sigma)
    evidence, n_samples = _sample_evidence(lat, lon, level, conn,
                                           candidate_ncs, radius, lam, alpha)

    # Posterior ∝ prior × evidence
    raw = {nc: prior.get(nc, 0) * evidence.get(nc, 0) for nc in candidate_ncs}
    total = sum(raw.values())
    if total == 0:
        return None
    posterior = {nc: p / total for nc, p in raw.items()}

    best_nc = max(posterior, key=posterior.get)

    return {
        'ncessch': best_nc,
        'school_name': school_names.get(best_nc, ''),
        'posterior': round(posterior[best_nc], 4),
        'prior': round(prior.get(best_nc, 0), 4),
        'evidence': round(evidence.get(best_nc, 0), 4),
        'n_samples': n_samples,
    }


def classify_point_simple(lat, lon, level, conn):
    """Convenience wrapper: looks up schools in the nearest district, classifies.

    Returns the same dict as classify_point, or None."""
    import db

    # Find which district this point is in
    try:
        from scripts.school_district_lookup import lookup_coords
        result = lookup_coords(lat, lon)
    except ImportError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from school_district_lookup import lookup_coords
        result = lookup_coords(lat, lon)

    if not result or not result.get('school_districts'):
        return None

    sd = result['school_districts']
    leaid = None
    for key in ('unified', 'elementary', 'secondary'):
        if key in sd:
            leaid = sd[key]['geoid']
            break
    if not leaid:
        return None

    schools = conn.execute(
        'SELECT s.ncessch, s.name, s.lat, s.lon, s.enrollment '
        'FROM schools s '
        'LEFT JOIN school_ratings r ON r.ncessch = s.ncessch '
        'WHERE s.leaid = ? AND s.level = ? '
        "AND COALESCE(r.source, '') NOT IN ('not-rated-alt', 'not-rated-pk') "
        'AND s.lat IS NOT NULL AND s.lon IS NOT NULL',
        (leaid, level)).fetchall()

    if not schools:
        return None

    return classify_point(lat, lon, schools, level, conn)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', default='MA')
    ap.add_argument('--level', default='elementary')
    ap.add_argument('--sigma', type=float, default=SIGMA_MILES)
    ap.add_argument('--lambda', type=float, default=LAMBDA_MILES, dest='lam')
    ap.add_argument('--alpha', type=float, default=ALPHA)
    ap.add_argument('--lat', type=float, help='classify a single point')
    ap.add_argument('--lon', type=float)
    args = ap.parse_args()

    import db
    conn = sqlite3.connect(db.DB_PATH)

    if args.lat and args.lon:
        result = classify_point_simple(args.lat, args.lon, args.level, conn)
        if result:
            print(f"School: {result['school_name']} ({result['ncessch']})")
            print(f"Posterior: {result['posterior']:.3f}  "
                  f"Prior: {result['prior']:.3f}  "
                  f"Evidence: {result['evidence']:.3f}  "
                  f"Samples: {result['n_samples']}")
        else:
            print("Could not classify")
    else:
        print("Use --lat and --lon to classify a point, or import as module")
