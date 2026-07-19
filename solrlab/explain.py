"""Translate Solr's debug=true output into something a human can learn from.

`solrlab explain --collection c "q=title_t:merge&fq=cat_s:x"` runs the query
with debugging on and renders three stories the raw JSON buries:

  1. what your query BECAME after parsing (analysis surprises live here)
  2. where the time went, per search component, prepare vs process
  3. why the top document scored what it did, as an indented tree

Solr-only: this reads Solr's debug component. (ES has the Profile API; a
translator for it is a natural follow-up.)
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl

import httpx


def fetch_debug(base_url: str, collection: str, query_string: str) -> dict:
    params = parse_qsl(query_string, keep_blank_values=True)
    params += [("debug", "true"), ("debug.explain.structured", "false"), ("wt", "json")]
    r = httpx.get(f"{base_url}/{collection}/select", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def format_parsed(debug: dict) -> str:
    lines = ["— what your query became —"]
    raw = debug.get("rawquerystring", "")
    parsed = debug.get("parsedquery_toString") or debug.get("parsedquery", "")
    lines.append(f"  you wrote:   {raw}")
    lines.append(f"  solr ran:    {parsed}")
    if raw and parsed and raw.split(":")[-1].lower() not in str(parsed).lower():
        lines.append("  (they differ — analysis chain at work: tokenization, "
                     "lowercasing, stemming, synonyms)")
    fdq = debug.get("filter_queries")
    if fdq:
        lines.append(f"  filters:     {fdq}  (cached separately in filterCache, "
                     "no score influence)")
    return "\n".join(lines)


def format_timing(debug: dict) -> str:
    timing = debug.get("timing", {})
    if not timing:
        return "— timing: not present in response —"
    total = timing.get("time", 0)
    lines = [f"— where the time went (total {total} ms) —"]
    for phase in ("prepare", "process"):
        ph = timing.get(phase, {})
        parts = [(name, comp.get("time", 0)) for name, comp in ph.items()
                 if isinstance(comp, dict)]
        parts.sort(key=lambda x: -x[1])
        shown = ", ".join(f"{n} {t}ms" for n, t in parts if t > 0) or "all ~0ms"
        lines.append(f"  {phase:<8} {ph.get('time', 0):>5} ms   ({shown})")
    biggest = max(
        ((n, c.get("time", 0)) for n, c in timing.get("process", {}).items()
         if isinstance(c, dict)),
        key=lambda x: x[1], default=(None, 0))
    if biggest[0] and total and biggest[1] > total * 0.5:
        lines.append(f"  >> '{biggest[0]}' dominates — that's your optimization target")
    return "\n".join(lines)


_INDENT = re.compile(r"^(\s*)")


def format_explain(body: dict, max_lines: int = 18) -> str:
    debug = body.get("debug", {})
    explain = debug.get("explain", {})
    docs = body.get("response", {}).get("docs", [])
    if not explain or not docs:
        return "— score explanation: no matching documents —"
    top_id = str(docs[0].get("id", next(iter(explain))))
    text = str(explain.get(top_id, next(iter(explain.values()))))
    lines = [f"— why doc '{top_id}' scored what it did —"]
    kept = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        depth = len(_INDENT.match(raw).group(1)) // 2
        if depth > 3:
            continue  # the deep leaves are tf/idf plumbing; the shape is up top
        lines.append("  " + "  " * depth + raw.strip())
        kept += 1
        if kept >= max_lines:
            lines.append("  ... (deeper detail elided — rerun with debug=true "
                         "yourself for the full tree)")
            break
    lines.append("  reading it: 'sum of' adds clauses, 'weight(...)' is one "
                 "term's contribution, 'boost' multiplies.")
    return "\n".join(lines)


def explain_report(body: dict) -> str:
    debug = body.get("debug", {})
    n = body.get("response", {}).get("numFound", 0)
    qtime = body.get("responseHeader", {}).get("QTime", "?")
    return "\n\n".join([
        f"numFound {n}, QTime {qtime} ms",
        format_parsed(debug),
        format_timing(debug),
        format_explain(body),
    ])
