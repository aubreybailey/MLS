#!/usr/bin/env python3
"""
Embedding-based school name matching via an OpenAI-compatible embeddings API.

Falls back gracefully if the endpoint is unreachable — never raises, just
returns None. Designed as a third-pass matcher after best_match and
match_by_distinctive_token, catching abbreviations, misspellings, and
variant forms that rule-based matching rejects as ambiguous.

Endpoint config (in priority order):
  1. EMBEDDING_URL env var
  2. --embedding-url CLI arg (in callers)
  3. Default: http://192.168.122.162:8082/v1/embeddings

Reference models (any OpenAI-compatible embedding server works):
  - mxbai-embed-large (1024-dim, good quality, runs locally via ollama/vllm)
  - all-MiniLM-L6-v2 (384-dim, 22MB, fast CPU inference via sentence-transformers)
"""

import json
import os
import urllib.request
from functools import lru_cache

DEFAULT_URL = 'http://192.168.122.162:8082/v1/embeddings'
ENDPOINT = os.environ.get('EMBEDDING_URL', DEFAULT_URL)

COSINE_THRESHOLD = 0.76
MIN_SEPARATION = 0.05


def _embed(texts, endpoint=None):
    """Get embeddings for a list of texts. Returns list of float vectors, or None on error."""
    url = endpoint or ENDPOINT
    payload = json.dumps({'input': texts, 'model': 'mxbai-embed-large'}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = [None] * len(texts)
        for item in data['data']:
            result[item['index']] = item['embedding']
        return result
    except Exception:
        return None


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@lru_cache(maxsize=4096)
def _cached_embed_single(text, endpoint=None):
    """Cache individual embeddings to avoid re-encoding the same school name."""
    result = _embed([text], endpoint)
    if result and result[0]:
        return tuple(result[0])
    return None


def embedding_match(query_name, candidates, endpoint=None, threshold=None,
                    min_separation=None):
    """Match a school name against NCES candidates using embedding similarity.

    candidates: list of dicts with at least 'name' and 'ncessch' keys
                (same format as db.schools_near() output)

    Returns the best candidate dict if similarity exceeds threshold and has
    clear separation from the runner-up, else None.
    """
    if not candidates or not query_name:
        return None

    thresh = threshold or COSINE_THRESHOLD
    sep = min_separation or MIN_SEPARATION

    all_texts = [query_name] + [c['name'] for c in candidates]
    vecs = _embed(all_texts, endpoint)
    if vecs is None or vecs[0] is None:
        return None

    query_vec = vecs[0]
    scores = []
    for i, cand in enumerate(candidates):
        cand_vec = vecs[i + 1]
        if cand_vec is None:
            continue
        sim = _cosine(query_vec, cand_vec)
        scores.append((sim, cand))

    if not scores:
        return None

    scores.sort(key=lambda x: -x[0])
    best_sim, best_cand = scores[0]

    if best_sim < thresh:
        return None

    if len(scores) >= 2:
        runner_sim = scores[1][0]
        if best_sim - runner_sim < sep:
            return None

    return best_cand


def batch_embedding_match(items, endpoint=None, threshold=None,
                          min_separation=None):
    """Match multiple (query_name, candidates) pairs efficiently.

    items: list of (query_name, candidates_list) tuples
    Returns: list of (matched_candidate_or_None, similarity_or_0) tuples

    Batches all texts into a single API call for efficiency."""
    if not items:
        return []

    thresh = threshold or COSINE_THRESHOLD
    sep = min_separation or MIN_SEPARATION

    all_texts = []
    index_map = []

    for i, (query, cands) in enumerate(items):
        q_idx = len(all_texts)
        all_texts.append(query)
        cand_indices = []
        for c in cands:
            cand_indices.append(len(all_texts))
            all_texts.append(c['name'])
        index_map.append((q_idx, cand_indices, cands))

    if len(all_texts) > 2000:
        results = []
        for query, cands in items:
            hit = embedding_match(query, cands, endpoint, threshold, min_separation)
            sim = 0.0
            if hit:
                qv = _embed([query], endpoint)
                cv = _embed([hit['name']], endpoint)
                if qv and cv and qv[0] and cv[0]:
                    sim = _cosine(qv[0], cv[0])
            results.append((hit, sim))
        return results

    vecs = _embed(all_texts, endpoint)
    if vecs is None:
        return [(None, 0.0)] * len(items)

    results = []
    for q_idx, cand_indices, cands in index_map:
        q_vec = vecs[q_idx]
        if q_vec is None:
            results.append((None, 0.0))
            continue

        scores = []
        for ci, cand in zip(cand_indices, cands):
            c_vec = vecs[ci]
            if c_vec is None:
                continue
            sim = _cosine(q_vec, c_vec)
            scores.append((sim, cand))

        if not scores:
            results.append((None, 0.0))
            continue

        scores.sort(key=lambda x: -x[0])
        best_sim, best_cand = scores[0]

        if best_sim < thresh:
            results.append((None, 0.0))
            continue

        if len(scores) >= 2 and best_sim - scores[1][0] < sep:
            results.append((None, 0.0))
            continue

        results.append((best_cand, best_sim))

    return results
