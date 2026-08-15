"""Run one hand-built query and report what Solr did with it.

The load generator answers "how does it behave under pressure". This
answers the other half — "what does this query actually return, and what
did the engine make of it" — which is where query syntax, parsers and
faceting are learned.

Two things are deliberately surfaced rather than hidden: the exact URL
that was sent (so the UI is a way to learn Solr's API, not a substitute
for knowing it), and the parsed query from debug output, which is where
dismax/edismax stop being mysterious.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

PARSERS = ("lucene", "dismax", "edismax")
MAX_ROWS = 100
MAX_START = 10_000       # past this, deep paging wants cursorMark instead

DISMAX = ("dismax", "edismax")

# The dismax family's scoring knobs, and which parsers actually read them.
#
# Solr answers an unknown parameter by ignoring it, so `mm` sent to the
# lucene parser looks like it worked and changes nothing. That silence is
# the thing worth guarding against here: a control that does nothing reads
# as a control that did nothing useful, which is a different lesson.
#
# (form key, Solr param, parsers that accept it)
SCORING_PARAMS = (
    ("minimum_should_match", "mm", DISMAX),
    ("tie_breaker", "tie", DISMAX),
    ("qs", "qs", DISMAX),
    ("pf", "pf", DISMAX),
    ("ps", "ps", DISMAX),
    ("pf2", "pf2", ("edismax",)),
    ("ps2", "ps2", ("edismax",)),
    ("pf3", "pf3", ("edismax",)),
    ("ps3", "ps3", ("edismax",)),
    ("bq", "bq", DISMAX),
    ("bf", "bf", DISMAX),
    ("boost", "boost", ("edismax",)),
)

# The ones that are numbers, and the range Solr will accept. Slop is a term
# distance, so it is a whole number; tie is a weight between 0 and 1.
NUMERIC_PARAMS = {
    "tie_breaker": ("tie", 0.0, 1.0, float),
    "qs": ("qs", 0, 100, int),
    "ps": ("ps", 0, 100, int),
    "ps2": ("ps2", 0, 100, int),
    "ps3": ("ps3", 0, 100, int),
}

# Each phrase slop only means anything alongside the phrase fields it
# applies to: ps with no pf is slop on a phrase query that was never built.
SLOP_NEEDS_FIELDS = {"ps": "pf", "ps2": "pf2", "ps3": "pf3"}


def list_fields(spec: ClusterSpec, collection: str, timeout: float = 15.0) -> list[dict]:
    """Indexed fields and their types, so the UI can offer real choices.

    Luke reports what is actually in the index, so an empty collection
    yields nothing — that's honest rather than broken.
    """
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{collection}/admin/luke",
                       params={"numTerms": 0, "wt": "json"})
        r.raise_for_status()
        fields = r.json().get("fields", {})
    out = []
    for name, info in sorted(fields.items()):
        if name.startswith("_"):          # _version_, _root_: plumbing
            continue
        ftype = info.get("type", "")
        out.append({
            "name": name,
            "type": ftype,
            # text fields are analyzed, so they suit full-text q/qf; the
            # rest are exact-match and are what faceting/sorting want
            "text": ftype.startswith("text"),
        })
    return out


def _text(body: dict, key: str) -> str:
    return str(body.get(key) or "").strip()


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


def _operator(body: dict) -> str | None:
    """q.op — whether the terms in q are all required or any one will do."""
    op = _text(body, "operator").upper()
    if not op:
        return None
    if op not in ("AND", "OR"):
        raise ValueError("Operator must be AND or OR.")
    return op


def _scoring(body: dict, parser: str) -> dict:
    """The dismax-family params, checked against the parser that will read them.

    Everything here is optional; what is not optional is that a param which
    made it into the request is one the parser will actually act on.
    """
    out: dict = {}
    for key, param, parsers in SCORING_PARAMS:
        if key in NUMERIC_PARAMS:
            _, lo, hi, cast = NUMERIC_PARAMS[key]
            value = _num(body, key, param, lo, hi, cast=cast)
            if value is None:
                continue
        else:
            value = _text(body, key)
            if not value:
                continue
        if parser not in parsers:
            raise ValueError(
                f"“{param}” belongs to {' and '.join(parsers)}, and this query "
                f"is using {parser}. Switch parser, or clear {param}.")
        out[param] = value

    for slop, fields in SLOP_NEEDS_FIELDS.items():
        if slop in out and fields not in out:
            raise ValueError(
                f"“{slop}” is the slop on the phrase {fields} builds, so on its "
                f"own it changes nothing. Set {fields} to the fields whose "
                f"phrase matches should be boosted.")
    return out


def build_params(body: dict, embedder=None) -> dict:
    """Turn the builder's form into Solr params, dropping anything blank.

    With `semantic` set, the query text is embedded and becomes a kNN
    search instead of a keyword one — the same box, a different question:
    "documents that mean this" rather than "documents containing this".
    """
    q = (body.get("q") or "").strip() or "*:*"
    parser = body.get("parser") or "lucene"

    if body.get("semantic"):
        if embedder is None:
            raise ValueError("Load an embedding model first.")
        if q == "*:*":
            raise ValueError("Semantic search needs something to search for.")
        from .embeddings import knn_query

        vector = embedder.embed_one(q)
        top_k = int(body.get("top_k") or body.get("rows") or 10)
        q = knn_query(body.get("vector_field") or "vec", vector, top_k)
        parser = "lucene"          # {!knn} is local-params, not a defType

    if parser not in PARSERS:
        raise ValueError(f"Parser must be one of {', '.join(PARSERS)}.")
    try:
        rows = int(body.get("rows") or 10)
    except (TypeError, ValueError):
        raise ValueError("Rows must be a number.") from None
    if not 0 <= rows <= MAX_ROWS:
        raise ValueError(f"Rows must be between 0 and {MAX_ROWS}.")

    params: dict = {"q": q, "rows": rows, "wt": "json"}
    start = _num(body, "start", "Start", 0, MAX_START, cast=int)
    if start:
        params["start"] = start
    # q.op is read by the lucene parser too, so it sits outside the block below
    op = _operator(body)
    if op:
        params["q.op"] = op
    if parser != "lucene":
        params["defType"] = parser
        qf = (body.get("qf") or "").strip()
        if qf:
            params["qf"] = qf
    params.update(_scoring(body, parser))

    hl_fields = _text(body, "highlight").replace(",", " ").split()
    if hl_fields:
        params["hl"] = "true"
        params["hl.fl"] = ",".join(hl_fields)
    sort = _text(body, "sort")
    if sort:
        params["sort"] = sort
    # Solr returns the score only when fl asks for it, unlike ES which puts
    # _score on every hit. Without it the boosting params above are invisible
    # — you can see the order change but not by how much, which is most of
    # what there is to learn from them.
    fl = _text(body, "fl")
    params["fl"] = f"{fl},score" if fl else "*,score"
    # repeated fq is meaningful in Solr, so keep them as a list
    fqs = [f.strip() for f in (body.get("fq") or []) if f and f.strip()]
    if fqs:
        params["fq"] = fqs

    facet_fields = [f for f in (body.get("facet_fields") or []) if f]
    if facet_fields:
        params["facet"] = "true"
        params["facet.field"] = facet_fields
        params["facet.limit"] = int(body.get("facet_limit") or 10)
        params["facet.mincount"] = 1
    if body.get("explain"):
        # debug=true (not debugQuery) also returns the timing breakdown, which
        # is the part that shows *where* the time went rather than just what
        # the query became.
        params["debug"] = "true"
        params["debug.explain.structured"] = "false"
    return params


def _timing(debug: dict) -> dict | None:
    """Per-component timing, sorted so the expensive part is obvious."""
    timing = debug.get("timing") or {}
    if not timing:
        return None
    phases = []
    for phase in ("prepare", "process"):
        block = timing.get(phase) or {}
        comps = [{"name": n, "time": c.get("time", 0)}
                 for n, c in block.items() if isinstance(c, dict)]
        comps.sort(key=lambda c: -c["time"])
        phases.append({"name": phase, "time": block.get("time", 0),
                       "components": comps})
    return {"total": timing.get("time", 0), "phases": phases}


def run_query(spec: ClusterSpec, collection: str, body: dict,
              timeout: float = 60.0, embedder=None) -> dict:
    """Execute the built query and normalize the parts worth showing."""
    params = build_params(body, embedder)
    url = f"{spec.base_url()}/{collection}/select"
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, params=params)
        sent = str(r.url)
        if r.status_code != 200:
            # Solr puts the useful part (syntax errors especially) in the body
            try:
                msg = r.json().get("error", {}).get("msg") or r.text[:300]
            except ValueError:
                msg = r.text[:300]
            return {"ok": False, "error": msg, "url": sent}
        data = r.json()

    resp = data.get("response", {})
    facets = {}
    raw_facets = (data.get("facet_counts") or {}).get("facet_fields") or {}
    for field, flat in raw_facets.items():
        # Solr returns [value, count, value, count, ...]
        facets[field] = [{"value": flat[i], "count": flat[i + 1]}
                         for i in range(0, len(flat), 2)]

    out = {
        "ok": True,
        "url": sent,
        "num_found": resp.get("numFound"),
        "qtime": (data.get("responseHeader") or {}).get("QTime"),
        "docs": resp.get("docs", []),
        "facets": facets,
        # id -> field -> fragments. Solr returns this beside the documents
        # rather than on them, so it is keyed by id for the UI to line up.
        "highlights": data.get("highlighting") or {},
        "raw": data,          # the untouched response, for the raw panel
    }
    debug = data.get("debug") or {}
    if debug:
        out["parsed"] = debug.get("parsedquery_toString") or debug.get("parsedquery")
        out["debug"] = {
            "raw_query": debug.get("rawquerystring"),
            "parsed": out["parsed"],
            "filters": debug.get("filter_queries"),
            "parser": debug.get("QParser"),
            "timing": _timing(debug),
            # id -> score explanation tree, as returned with structured=false
            "explain": debug.get("explain") or {},
        }
    return out
