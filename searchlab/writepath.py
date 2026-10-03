"""The write path, one document at a time (Solr).

A bulk load hides every stage of a write. Indexing a single document and
asking the cluster about it after each step shows them: the analyzer turns
the text into terms, the add lands in the transaction log and an in-memory
buffer (real-time get can see it, search cannot), and only a commit or a
full buffer writes a segment. `segments.diff_segments` shows that last part.
"""

from __future__ import annotations

import time
import uuid

import httpx

from .cluster import ClusterSpec


def _short(cls: str) -> str:
    return cls.rsplit(".", 1)[-1]


def _pairs(raw) -> list[tuple[str, object]]:
    # Solr's default json.nl=flat encodes a NamedList as [k1, v1, k2, v2, ...];
    # other json.nl settings give a dict or [[k, v], ...]
    if isinstance(raw, dict):
        return list(raw.items())
    if isinstance(raw, list) and raw and all(isinstance(x, list) and len(x) == 2
                                             and isinstance(x[0], str) for x in raw):
        return [(k, v) for k, v in raw]
    if isinstance(raw, list):
        return [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2)]
    return []


def parse_stages(raw) -> list[dict]:
    """One field's `index` analysis, as ordered stages with their tokens.

    A char filter's output is a single string (it rewrites text before
    tokenizing); tokenizers and token filters output token lists.
    """
    stages = []
    for name, out in _pairs(raw):
        if isinstance(out, str):
            tokens = [{"text": out, "position": None, "type": "text"}]
        else:
            tokens = [{"text": t.get("text"), "position": t.get("position"),
                       "type": t.get("type")} for t in (out or []) if isinstance(t, dict)]
        stages.append({"stage": _short(str(name)), "class": str(name), "tokens": tokens})
    return stages


def fetch_analysis(spec: ClusterSpec, collection: str, field: str, value: str,
                   timeout: float = 10.0) -> list[dict]:
    """What the index-time analyzer of `field` makes of `value`."""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{collection}/analysis/field",
                       params={"analysis.fieldname": field,
                               "analysis.fieldvalue": value, "wt": "json"})
        r.raise_for_status()
        body = r.json()
    names = (body.get("analysis") or {}).get("field_names") or {}
    entry = dict(_pairs(names)).get(field) or {}
    return parse_stages(dict(_pairs(entry)).get("index") or [])


def index_single_doc(spec: ClusterSpec, collection: str, field: str, value: str,
                     timeout: float = 15.0) -> dict:
    """Add one document with no commit, so it stays in the buffer until one."""
    doc_id = f"wp-{uuid.uuid4().hex[:12]}"
    req = spec.eng().bulk_request(spec.base_url(), collection,
                                  [{"id": doc_id, field: value}], None)
    # httpx would send commitWithin=None as an empty parameter, not omit it
    req["params"] = {k: v for k, v in req["params"].items() if v is not None}
    t0 = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        r = client.request(**req)
        r.raise_for_status()
    return {"id": doc_id, "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


def locate_doc(spec: ClusterSpec, collection: str, doc_id: str, cores: list[str],
               timeout: float = 10.0) -> str | None:
    """Which leader holds the document. Asked of each core alone
    (distrib=false), real-time get answers from that core's transaction log,
    so it knows before any commit. The walkthrough then watches that shard,
    rather than taking any new segment on any shard for this document."""
    with httpx.Client(timeout=timeout) as client:
        for core in cores:
            r = client.get(f"{spec.base_url()}/{core}/get",
                           params={"id": doc_id, "distrib": "false", "wt": "json"})
            if r.status_code == 200 and r.json().get("doc") is not None:
                return core
    return None


def commit_settings(spec: ClusterSpec, collection: str, timeout: float = 10.0) -> dict:
    """The automatic commits that could make the document flush or appear,
    so the walkthrough can name what did instead of assuming the soft one."""
    r = httpx.get(f"{spec.base_url()}/{collection}/config",
                  params={"wt": "json"}, timeout=timeout)
    r.raise_for_status()
    uh = (r.json().get("config") or {}).get("updateHandler") or {}
    soft, hard = uh.get("autoSoftCommit") or {}, uh.get("autoCommit") or {}
    return {"soft_ms": int(soft.get("maxTime") or -1),
            "hard_ms": int(hard.get("maxTime") or -1),
            "hard_opens_searcher": str(hard.get("openSearcher")).lower() == "true"}


def doc_visibility(spec: ClusterSpec, collection: str, doc_id: str,
                   timeout: float = 10.0) -> dict:
    """Two answers to "is it there yet?", which differ until a commit.

    Real-time get reads the transaction log, so it sees an add immediately.
    Search reads the current searcher's segments, so it sees it only after
    a (soft or hard) commit has opened a new searcher.
    """
    with httpx.Client(timeout=timeout) as client:
        rtg = client.get(f"{spec.base_url()}/{collection}/get",
                         params={"id": doc_id, "wt": "json"})
        rtg.raise_for_status()
        sel = client.get(f"{spec.base_url()}/{collection}/select",
                         params={"q": "id:" + _escape(doc_id), "rows": 0, "wt": "json"})
        sel.raise_for_status()
    doc = rtg.json().get("doc")
    found = ((sel.json().get("response") or {}).get("numFound") or 0) > 0
    return {"rtg": doc is not None, "searchable": found, "doc": doc}


def _escape(term: str) -> str:
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/ ' else c for c in term)
