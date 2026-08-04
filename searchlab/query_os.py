"""Run one hand-built query against OpenSearch / Elasticsearch.

The counterpart of query.py. Same three things the Solr version surfaces —
the request that was actually sent, what the engine made of it, and where
the time went — expressed in the query DSL instead of Solr's params.

The parser choices map onto real DSL shapes rather than being invented:

    lucene   -> query_string      the same Lucene syntax, field:value and all
    edismax  -> multi_match       one phrase across weighted fields ("title^3")
    semantic -> knn               nearest vectors to an embedded query

Facets are terms aggregations, filters are a bool filter clause (which,
like Solr's fq, is not scored), and "explain" asks for both the rewritten
query and a per-shard timing profile.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

PARSERS = ("lucene", "dismax", "edismax")
MAX_ROWS = 100

# mapping types that behave like Solr's analyzed text: good for full-text
# search, wrong for exact-match faceting
_TEXT_TYPES = {"text", "match_only_text", "search_as_you_type", "annotated_text"}


def list_fields(spec: ClusterSpec, index: str, timeout: float = 15.0) -> list[dict]:
    """Fields and their types, so the UI can offer real choices."""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{index}/_mapping")
        r.raise_for_status()
        body = r.json()
    block = body.get(index) or next(iter(body.values()), {})
    props = (block.get("mappings") or {}).get("properties") or {}

    out = []
    for name, cfg in sorted(props.items()):
        ftype = cfg.get("type", "object")
        is_text = ftype in _TEXT_TYPES
        out.append({"name": name, "type": ftype, "text": is_text})
        # a text field usually carries a .keyword subfield, and *that* is
        # what aggregations need — surface it so faceting is possible at all
        for sub, subcfg in (cfg.get("fields") or {}).items():
            out.append({"name": f"{name}.{sub}",
                        "type": subcfg.get("type", "keyword"), "text": False})
    return out


def _filters(body: dict) -> list[dict]:
    """Each fq line becomes a non-scoring filter clause, as in Solr."""
    out = []
    for line in body.get("fq") or []:
        line = (line or "").strip()
        if line:
            out.append({"query_string": {"query": line}})
    return out


def build_body(body: dict, embedder=None) -> dict:
    """Turn the builder's form into a search body."""
    q = (body.get("q") or "").strip()
    parser = body.get("parser") or "lucene"

    try:
        size = int(body.get("rows") or 10)
    except (TypeError, ValueError):
        raise ValueError("Rows must be a number.") from None
    if not 0 <= size <= MAX_ROWS:
        raise ValueError(f"Rows must be between 0 and {MAX_ROWS}.")

    out: dict = {"size": size}

    if body.get("semantic"):
        if embedder is None:
            raise ValueError("Load an embedding model first.")
        if not q or q == "*:*":
            raise ValueError("Semantic search needs something to search for.")
        field = body.get("vector_field") or "vec"
        k = int(body.get("top_k") or size or 10)
        inner: dict = {"knn": {field: {"vector": embedder.embed_one(q), "k": k}}}
    elif parser in ("dismax", "edismax"):
        qf = (body.get("qf") or "").strip()
        fields = qf.split() if qf else ["*"]
        inner = {"multi_match": {"query": q or "", "fields": fields}} if q else \
                {"match_all": {}}
    else:
        if parser not in PARSERS:
            raise ValueError(f"Parser must be one of {', '.join(PARSERS)}.")
        inner = {"query_string": {"query": q}} if q and q != "*:*" else {"match_all": {}}

    filters = _filters(body)
    out["query"] = {"bool": {"must": [inner], "filter": filters}} if filters else inner

    sort = (body.get("sort") or "").strip()
    if sort:
        # accept Solr's "field desc" spelling and translate it
        parts = sort.split()
        out["sort"] = [{parts[0]: {"order": parts[1]}} if len(parts) > 1
                       else parts[0]]
    fl = (body.get("fl") or "").strip()
    if fl:
        out["_source"] = [f.strip() for f in fl.split(",") if f.strip()]

    facets = [f for f in (body.get("facet_fields") or []) if f]
    if facets:
        limit = int(body.get("facet_limit") or 10)
        out["aggs"] = {f: {"terms": {"field": f, "size": limit}} for f in facets}
    if body.get("explain"):
        out["profile"] = True
    return out


def _deepest_reason(node: dict) -> str:
    """Follow caused_by to the explanation that actually names the problem."""
    reason = node.get("reason") or ""
    caused = node.get("caused_by")
    while isinstance(caused, dict):
        deeper = caused.get("reason")
        if deeper:
            reason = deeper
        caused = caused.get("caused_by")
    return reason


def _error_message(response) -> str:
    """A usable message from a search failure.

    The top-level reason is usually just "all shards failed"; the real
    explanation is nested inside the first shard failure, sometimes several
    caused_by levels down.
    """
    try:
        err = response.json().get("error") or {}
    except ValueError:
        return response.text[:300]
    if isinstance(err, str):
        return err
    msg = err.get("reason") or "search failed"
    shards = err.get("failed_shards") or []
    if shards:
        detail = _deepest_reason(shards[0].get("reason") or {})
        if detail:
            msg = detail
    # aggregating a text field is the most common trap here, and the engine's
    # own advice is buried at the end of a long paragraph
    if "Text fields are not optimised" in msg:
        msg = ("Cannot facet or sort on a text field — it is analysed into "
               "tokens. Use its .keyword sub-field instead (for example "
               "category.keyword rather than category).")
    return msg.strip()


def _timing(profile: dict) -> dict | None:
    """Per-component timing, sorted so the expensive part leads.

    The profile is per-shard and deeply nested; collapse it to the same
    shape the Solr side reports so one UI renders both.
    """
    shards = (profile or {}).get("shards") or []
    if not shards:
        return None
    query_ns: dict[str, int] = {}
    collector_ns = 0
    total = 0
    for shard in shards:
        for search in shard.get("searches", []):
            for q in search.get("query", []):
                kind = q.get("type", "query")
                query_ns[kind] = query_ns.get(kind, 0) + q.get("time_in_nanos", 0)
                total += q.get("time_in_nanos", 0)
            for c in search.get("collector", []):
                collector_ns += c.get("time_in_nanos", 0)
                total += c.get("time_in_nanos", 0)
    comps = [{"name": k, "time": round(v / 1e6, 2)} for k, v in query_ns.items()]
    comps.sort(key=lambda c: -c["time"])
    phases = [{"name": "query", "time": round(sum(c["time"] for c in comps), 2),
               "components": comps}]
    if collector_ns:
        phases.append({"name": "collect", "time": round(collector_ns / 1e6, 2),
                       "components": [{"name": "collector",
                                       "time": round(collector_ns / 1e6, 2)}]})
    return {"total": round(total / 1e6, 2), "phases": phases}


def _parsed(profile: dict) -> str | None:
    """What the engine rewrote the query into — the Lucene form it ran."""
    for shard in (profile or {}).get("shards") or []:
        for search in shard.get("searches", []):
            for q in search.get("query", []):
                if q.get("description"):
                    return q["description"]
    return None


def run_query(spec: ClusterSpec, index: str, body: dict,
              timeout: float = 60.0, embedder=None) -> dict:
    """Execute the built query and normalize what's worth showing."""
    search = build_body(body, embedder)
    url = f"{spec.base_url()}/{index}/_search"
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=search)
        sent = str(r.url)
        if r.status_code >= 400:
            return {"ok": False, "error": _error_message(r), "url": sent,
                    "request": search}
        data = r.json()

    hits = data.get("hits") or {}
    total = hits.get("total")
    num_found = total.get("value") if isinstance(total, dict) else total

    facets = {}
    for field, agg in (data.get("aggregations") or {}).items():
        facets[field] = [{"value": b.get("key"), "count": b.get("doc_count")}
                         for b in agg.get("buckets", [])]

    docs = []
    for h in hits.get("hits", []):
        doc = dict(h.get("_source") or {})
        doc.setdefault("id", h.get("_id"))
        if h.get("_score") is not None:
            doc["score"] = h["_score"]
        docs.append(doc)

    out = {
        "ok": True,
        "url": sent,
        "request": search,            # the DSL body, since the URL alone hides it
        "num_found": num_found,
        "qtime": data.get("took"),
        "docs": docs,
        "facets": facets,
        "raw": data,
    }
    profile = data.get("profile")
    if profile:
        out["parsed"] = _parsed(profile)
        out["debug"] = {
            "raw_query": (body.get("q") or "").strip(),
            "parsed": out["parsed"],
            "filters": [f.get("query_string", {}).get("query")
                        for f in _filters(body)] or None,
            "parser": body.get("parser"),
            "timing": _timing(profile),
            "explain": {},
        }
    return out
