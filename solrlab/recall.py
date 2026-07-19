"""Recall measurement for approximate kNN: the other half of the ANN story.

Latency without recall is meaningless for vector search — HNSW will happily
answer fast and wrong. This module computes exact ground truth locally
(brute-force over the generated dataset, which solrlab has on disk anyway),
queries the engine's approximate kNN with the same vectors, and reports
recall@k alongside latency — across a sweep of `num_candidates` (ES) /
`ef_search`-style settings where the engine exposes one, since that knob IS
the recall/latency tradeoff.

Query vectors are drawn from the same clustered distribution as the data
(different seed), because recall against in-distribution queries is what
production will see.

Requires numpy for the ground-truth pass: `pip install solrlab[recall]`.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import httpx

from .datagen import FieldGen


def _np():
    try:
        import numpy
        return numpy
    except ImportError:
        sys.exit("solrlab: recall needs numpy — pip install 'solrlab[recall]' "
                 "(or pip install numpy)")


def find_vector_field(profile: dict) -> tuple[str, dict]:
    for name, cfg in profile["fields"].items():
        if (cfg or {}).get("type") == "vector":
            return name, cfg
    sys.exit("solrlab: profile has no vector field")


def load_vectors(data_path: str | Path, field: str):
    """(ids, float32 matrix) from a generated JSONL dataset."""
    np = _np()
    ids, rows = [], []
    with open(data_path) as f:
        for line in f:
            doc = json.loads(line)
            ids.append(str(doc["id"]))
            rows.append(doc[field])
    if not rows:
        sys.exit(f"solrlab: no docs in {data_path}")
    return ids, np.asarray(rows, dtype="float32")


def gen_query_vectors(field_cfg: dict, n: int, seed: int):
    """Draw query vectors from the same clustered distribution as the data.
    Same profile config, different seed: in-distribution but unseen."""
    import random

    np = _np()
    rng = random.Random(seed)
    gen = FieldGen("q", field_cfg, rng)
    return np.asarray([gen.value(i) for i in range(n)], dtype="float32")


def ground_truth(data, queries, k: int) -> list[set]:
    """Exact top-k ids by dot product (vectors are unit-normalized, so dot
    product == cosine). Returns a set per query for O(1) membership."""
    np = _np()
    sims = queries @ data.T                       # (Q, N)
    idx = np.argpartition(-sims, k, axis=1)[:, :k]
    return [set(row.tolist()) for row in idx]


def _knn_request(engine: str, base: str, collection: str, field: str,
                 vec: list[float], k: int, candidates: int | None) -> dict:
    if engine == "solr":
        vec_txt = "[" + ", ".join(str(round(x, 4)) for x in vec) + "]"
        return {"method": "GET", "url": f"{base}/{collection}/select",
                "params": {"q": f"{{!knn f={field} topK={k}}}{vec_txt}",
                           "fl": "id", "rows": k, "wt": "json"}}
    if engine == "elasticsearch":
        knn = {"field": field, "query_vector": vec, "k": k,
               "num_candidates": candidates or max(k * 5, 50)}
        return {"method": "POST", "url": f"{base}/{collection}/_search",
                "json": {"knn": knn, "size": k, "_source": False}}
    # opensearch
    return {"method": "POST", "url": f"{base}/{collection}/_search",
            "json": {"size": k, "_source": False,
                     "query": {"knn": {field: {"vector": vec, "k": k}}}}}


def _extract_ids(engine: str, body: dict) -> list[str]:
    if engine == "solr":
        return [str(d["id"]) for d in body.get("response", {}).get("docs", [])]
    return [str(h["_id"]) for h in body.get("hits", {}).get("hits", [])]


async def run_recall(
    engine: str, base_url: str, collection: str, field: str,
    ids: list[str], queries, truth: list[set], k: int,
    candidates: int | None = None, timeout: float = 30.0,
) -> dict:
    """One pass over the query set at one candidates setting."""
    recalls, latencies, errors = [], [], 0
    id_index = {i: ids[i] for i in range(len(ids))}
    truth_ids = [{id_index[i] for i in t} for t in truth]
    async with httpx.AsyncClient(timeout=timeout) as client:
        for q, expect in zip(queries.tolist(), truth_ids, strict=True):
            req = _knn_request(engine, base_url, collection, field, q, k, candidates)
            t0 = time.perf_counter()
            try:
                r = await client.request(**req)
                latencies.append((time.perf_counter() - t0) * 1000)
                if r.status_code != 200:
                    errors += 1
                    continue
                got = set(_extract_ids(engine, r.json()))
            except httpx.HTTPError:
                errors += 1
                continue
            recalls.append(len(got & expect) / k)
    lat = sorted(latencies)

    def pct(p):
        return round(lat[min(int(len(lat) * p / 100), len(lat) - 1)], 1) if lat else None

    return {
        "candidates": candidates,
        "queries": len(queries),
        "errors": errors,
        "recall_mean": round(statistics.mean(recalls), 4) if recalls else None,
        "recall_min": round(min(recalls), 4) if recalls else None,
        "lat_p50_ms": pct(50),
        "lat_p99_ms": pct(99),
    }


def format_results(results: list[dict], k: int) -> str:
    lines = [f"{'candidates':>10}  {'recall@' + str(k):>10}  {'min':>7}  "
             f"{'p50 ms':>7}  {'p99 ms':>7}  {'errors':>6}"]
    for r in results:
        cand = r["candidates"] if r["candidates"] is not None else "engine default"
        lines.append(f"{cand!s:>10}  {r['recall_mean']!s:>10}  {r['recall_min']!s:>7}  "
                     f"{r['lat_p50_ms']!s:>7}  {r['lat_p99_ms']!s:>7}  {r['errors']:>6}")
    return "\n".join(lines)
