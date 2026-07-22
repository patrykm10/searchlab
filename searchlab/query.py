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


def build_params(body: dict) -> dict:
    """Turn the builder's form into Solr params, dropping anything blank."""
    q = (body.get("q") or "").strip() or "*:*"
    parser = body.get("parser") or "lucene"
    if parser not in PARSERS:
        raise ValueError(f"Parser must be one of {', '.join(PARSERS)}.")
    try:
        rows = int(body.get("rows") or 10)
    except (TypeError, ValueError):
        raise ValueError("Rows must be a number.") from None
    if not 0 <= rows <= MAX_ROWS:
        raise ValueError(f"Rows must be between 0 and {MAX_ROWS}.")

    params: dict = {"q": q, "rows": rows, "wt": "json"}
    if parser != "lucene":
        params["defType"] = parser
        qf = (body.get("qf") or "").strip()
        if qf:
            params["qf"] = qf
    for key in ("sort", "fl"):
        val = (body.get(key) or "").strip()
        if val:
            params[key] = val
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
              timeout: float = 60.0) -> dict:
    """Execute the built query and normalize the parts worth showing."""
    params = build_params(body)
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
