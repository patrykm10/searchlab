"""Does the search find the right thing? Scored against the catalogue's own ground truth.

`recall` answers a narrower question: does approximate kNN return the same
neighbours brute force would. That is a property of the index, and it is
answerable with meaningless vectors — it never asks whether those neighbours
were the right documents.

This asks the other one. The catalogue knows what each document is, and each
benchmark query names the category that answers it, so a result is right or
wrong rather than merely near. Both retrieval paths are run over the same
queries, because the interesting number is not either score alone but the gap
between them: the queries are phrased the way someone would ask ("how do I
block out noise in an office") and share almost no words with the documents
that answer them, which is precisely where term matching runs out and
embeddings are supposed to earn their cost.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import httpx

from .catalog import benchmark_queries


def _precision_at_k(hits: list[str], relevant: list[str]) -> float:
    """Share of the returned documents that are the right kind of thing.

    Recall@k is the wrong summary here: a category holds thousands of
    documents and k is ten, so recall would be a fraction of a percent
    however good the search was. Precision answers what someone actually
    looks at — of what came back, how much of it belongs.
    """
    if not hits:
        return 0.0
    return sum(1 for h in hits if h in relevant) / len(hits)


def _search_es(client: httpx.Client, base: str, collection: str,
               body: dict) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    r = client.post(f"{base}/{collection}/_search", json=body, timeout=60)
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    cats = [h["_source"].get("category_s", "")
            for h in r.json().get("hits", {}).get("hits", [])]
    return cats, ms


def compare(spec, collection: str, model, k: int = 10,
            text_fields: tuple[str, ...] = ("title_t", "body_t"),
            vector_field: str = "vec") -> dict[str, Any]:
    """Run every benchmark query both ways and score each against its category.

    Returns per-query rows plus the aggregate, so a caller can print the
    summary and still show which queries the two disagree about — the
    disagreements are the argument.
    """
    base = spec.base_url(0)
    rows: list[dict[str, Any]] = []

    with httpx.Client(timeout=60) as client:
        for case in benchmark_queries():
            query, relevant = case["query"], case["relevant"]

            lex_body = {
                "size": k,
                "query": {"multi_match": {"query": query,
                                          "fields": list(text_fields)}},
                "_source": ["category_s"],
            }
            lex_hits, lex_ms = _search_es(client, base, collection, lex_body)

            vector = model.embed([query])[0]
            vec_body = {
                "size": k,
                "query": {"knn": {vector_field: {"vector": vector, "k": k}}},
                "_source": ["category_s"],
            }
            vec_hits, vec_ms = _search_es(client, base, collection, vec_body)

            rows.append({
                "query": query,
                "relevant": relevant,
                "lexical_p_at_k": _precision_at_k(lex_hits, relevant),
                "semantic_p_at_k": _precision_at_k(vec_hits, relevant),
                "lexical_ms": round(lex_ms, 1),
                "semantic_ms": round(vec_ms, 1),
                # a query that returns nothing at all is a different failure
                # from one that returns the wrong things, and the mean hides it
                "lexical_empty": not lex_hits,
            })

    lex = [r["lexical_p_at_k"] for r in rows]
    vec = [r["semantic_p_at_k"] for r in rows]
    return {
        "k": k,
        "queries": len(rows),
        "lexical_mean_p_at_k": round(statistics.fmean(lex), 3) if lex else 0.0,
        "semantic_mean_p_at_k": round(statistics.fmean(vec), 3) if vec else 0.0,
        "lexical_empty_results": sum(1 for r in rows if r["lexical_empty"]),
        "lexical_median_ms": round(statistics.median(
            [r["lexical_ms"] for r in rows]), 1) if rows else 0.0,
        "semantic_median_ms": round(statistics.median(
            [r["semantic_ms"] for r in rows]), 1) if rows else 0.0,
        "rows": rows,
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"{result['queries']} benchmark queries, precision@{result['k']} "
        "against the category that answers each one",
        "",
        f"  lexical   {result['lexical_mean_p_at_k']:.3f}   "
        f"median {result['lexical_median_ms']:.1f} ms"
        + (f"   ({result['lexical_empty_results']} returned nothing at all)"
           if result["lexical_empty_results"] else ""),
        f"  semantic  {result['semantic_mean_p_at_k']:.3f}   "
        f"median {result['semantic_median_ms']:.1f} ms",
        "",
    ]
    gap = sorted(result["rows"],
                 key=lambda r: r["semantic_p_at_k"] - r["lexical_p_at_k"],
                 reverse=True)
    lines.append("Widest gaps (semantic − lexical):")
    for r in gap[:5]:
        lines.append(f"  {r['semantic_p_at_k']:.2f} vs {r['lexical_p_at_k']:.2f}"
                     f"   {r['query']}")
    losses = [r for r in gap if r["semantic_p_at_k"] < r["lexical_p_at_k"]]
    if losses:
        lines.append("")
        lines.append("Queries where term matching did better:")
        for r in losses[:5]:
            lines.append(f"  {r['semantic_p_at_k']:.2f} vs {r['lexical_p_at_k']:.2f}"
                         f"   {r['query']}")
    return "\n".join(lines)
