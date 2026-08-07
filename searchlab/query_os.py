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
MAX_FROM = 10_000        # the default index.max_result_window

# The DSL query types the builder can produce. Solr reaches most of these
# through one parser and a pile of local params; here they are separate
# clauses, so the builder offers them as separate choices.
QTYPES = ("query_string", "multi_match", "match", "match_phrase", "term", "knn")

# multi_match is really six queries behind one name, and the type is what
# picks between them — it is the single most consequential knob here.
MM_TYPES = ("best_fields", "most_fields", "cross_fields",
            "phrase", "phrase_prefix", "bool_prefix")

# what the old parser names mean in DSL terms, so existing callers and
# saved queries keep working
_PARSER_QTYPE = {"lucene": "query_string", "dismax": "multi_match",
                 "edismax": "multi_match"}

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


def _num(body: dict, key: str, label: str, lo: float, hi: float,
         default=None, cast=float):
    """Read an optional numeric field, or explain what is wrong with it."""
    raw = body.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not lo <= value <= hi:
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}.")
    return value


def _fields(body: dict) -> list[str]:
    """multi_match's field list. `title^3 body` is the DSL's own notation for
    a boost, so it is passed through rather than translated."""
    raw = (body.get("fields") or body.get("qf") or "").strip()
    return raw.split() if raw else ["*"]


def _qtype(body: dict) -> str:
    """Which DSL clause to build.

    `qtype` names it directly. The older `parser`/`semantic` keys are still
    honoured so saved queries and the Solr-shaped form keep working.
    """
    if body.get("semantic"):
        return "knn"
    qtype = (body.get("qtype") or "").strip()
    if qtype:
        if qtype not in QTYPES:
            raise ValueError(f"Query type must be one of {', '.join(QTYPES)}.")
        return qtype
    parser = body.get("parser") or "lucene"
    if parser not in PARSERS:
        raise ValueError(f"Parser must be one of {', '.join(PARSERS)}.")
    return _PARSER_QTYPE[parser]


def _inner_query(body: dict, qtype: str, size: int, embedder=None) -> dict:
    """The scoring clause, before filters wrap it."""
    q = (body.get("q") or "").strip()
    blank = not q or q == "*:*"

    if qtype == "knn":
        if embedder is None:
            raise ValueError("Load an embedding model first.")
        if blank:
            raise ValueError("Semantic search needs something to search for.")
        field = body.get("vector_field") or "vec"
        k = int(body.get("top_k") or size or 10)
        return {"knn": {field: {"vector": embedder.embed_one(q), "k": k}}}

    if blank:
        return {"match_all": {}}

    if qtype == "query_string":
        clause: dict = {"query": q}
        _put(clause, "default_operator", _operator(body))
        _put(clause, "fields", body.get("fields") and _fields(body))
        return {"query_string": clause}

    if qtype == "multi_match":
        clause = {"query": q, "fields": _fields(body)}
        mm_type = (body.get("mm_type") or "").strip()
        if mm_type:
            if mm_type not in MM_TYPES:
                raise ValueError(
                    f"multi_match type must be one of {', '.join(MM_TYPES)}.")
            clause["type"] = mm_type
        _put(clause, "operator", _operator(body))
        _put(clause, "minimum_should_match", _mm(body))
        _put(clause, "fuzziness", _fuzziness(body, mm_type))
        _put(clause, "tie_breaker",
             _num(body, "tie_breaker", "Tie breaker", 0, 1))
        _put(clause, "slop", _num(body, "slop", "Slop", 0, 100, cast=int)
             if mm_type in ("phrase", "phrase_prefix") else None)
        return {"multi_match": clause}

    field = (body.get("field") or "").strip()
    if not field:
        raise ValueError(f"A {qtype} query needs one field to search.")

    if qtype == "match":
        clause = {"query": q}
        _put(clause, "operator", _operator(body))
        _put(clause, "minimum_should_match", _mm(body))
        _put(clause, "fuzziness", _fuzziness(body, None))
        return {"match": {field: clause}} if len(clause) > 1 else \
               {"match": {field: q}}

    if qtype == "match_phrase":
        clause = {"query": q}
        _put(clause, "slop", _num(body, "slop", "Slop", 0, 100, cast=int))
        return {"match_phrase": {field: clause}} if len(clause) > 1 else \
               {"match_phrase": {field: q}}

    # term: no analysis at all, which is why it belongs on a keyword field
    return {"term": {field: {"value": q}}}


def _operator(body: dict) -> str | None:
    op = (body.get("operator") or "").strip().upper()
    if not op:
        return None
    if op not in ("AND", "OR"):
        raise ValueError("Operator must be AND or OR.")
    return op


def _mm(body: dict) -> str | None:
    """minimum_should_match takes counts and percentages, so it stays a
    string rather than being forced into a number."""
    value = (str(body.get("minimum_should_match") or "")).strip()
    return value or None


def _fuzziness(body: dict, mm_type: str | None) -> str | None:
    value = (str(body.get("fuzziness") or "")).strip()
    if not value:
        return None
    if value.upper() != "AUTO" and value not in ("0", "1", "2"):
        raise ValueError("Fuzziness must be AUTO, 0, 1 or 2.")
    # the phrase types run the terms in sequence and reject fuzziness
    # outright; saying so beats a shard failure
    if mm_type in ("phrase", "phrase_prefix"):
        raise ValueError(
            f"multi_match type “{mm_type}” cannot use fuzziness — it matches "
            f"terms in sequence. Use best_fields or most_fields instead.")
    return value.upper() if value.upper() == "AUTO" else value


def _put(target: dict, key: str, value) -> None:
    """Set a DSL key only when it has a value, so the previewed JSON shows
    what was actually asked for and not a wall of defaults."""
    if value not in (None, "", [], {}):
        target[key] = value


def _highlight(body: dict) -> dict | None:
    raw = (body.get("highlight") or "").strip()
    if not raw:
        return None
    fields = [f.strip() for f in raw.replace(",", " ").split() if f.strip()]
    return {"fields": {f: {} for f in fields}} if fields else None


def build_body(body: dict, embedder=None) -> dict:
    """Turn the builder's form into a search body."""
    size = int(_num(body, "rows", "Rows", 0, MAX_ROWS, default=10, cast=int))
    qtype = _qtype(body)

    out: dict = {"size": size}
    _put(out, "from", _num(body, "from", "From", 0, MAX_FROM, cast=int))

    inner = _inner_query(body, qtype, size, embedder)
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

    _put(out, "highlight", _highlight(body))
    _put(out, "min_score", _num(body, "min_score", "Minimum score", 0, 1e6))
    # without this the hit count silently stops being exact past 10,000 and
    # starts reporting "gte" — a number people quote in reports
    if body.get("track_total_hits"):
        out["track_total_hits"] = True
    if body.get("explain"):
        out["profile"] = True
    return out


class _PlaceholderEmbedder:
    """Stands in for a real model so a semantic query can still be previewed.

    The preview exists to show which clause the controls produce; refusing
    to draw the knn shape until a model is downloaded would withhold exactly
    the thing being asked for.
    """

    dims = 0

    def embed_one(self, text):
        return ["<vector from the embedding model>"]


def preview_body(body: dict, embedder=None) -> dict:
    """The same body, with vectors abbreviated so it can be read.

    A 384-float vector is the whole preview otherwise, and the shape is the
    point — not the numbers.
    """
    import copy

    if embedder is None and body.get("semantic"):
        embedder = _PlaceholderEmbedder()
    out = copy.deepcopy(build_body(body, embedder))
    _shorten_vectors(out)
    return out


def _shorten_vectors(node) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "vector" and isinstance(value, list) and len(value) > 6:
                node[key] = [round(v, 4) for v in value[:3]] + \
                            [f"…{len(value)} floats"]
            else:
                _shorten_vectors(value)
    elif isinstance(node, list):
        for item in node:
            _shorten_vectors(item)


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
