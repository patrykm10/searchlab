"""Add embeddings to documents already indexed in OpenSearch / Elasticsearch.

The counterpart of vectorize.py. Same shape — read the documents back,
embed a text field here, write the vectors on — but two things differ
enough to matter.

**Enabling vectors is not free on an existing index.** OpenSearch gates
approximate k-NN behind `index.knn`, which is a *static* setting: it can
only be set while the index is closed. So an index that was not created
with vectors in mind has to be closed, reconfigured, and reopened before
it can hold one — briefly unavailable, which is the whole lesson. Solr
has no equivalent step; you just add the field. That asymmetry is worth
seeing, so it is reported rather than hidden.

**Deep pagination works differently.** Solr's cursorMark is a stateless
token. The equivalent here is the scroll API, which is *stateful*: the
server pins a view of the index and hands back a scroll id, so the search
context has to be released afterwards or it occupies heap until it times
out. That release is in a `finally` for exactly that reason.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

BATCH = 200
SCROLL_TTL = "2m"


def knn_enabled(spec: ClusterSpec, index: str, timeout: float = 30.0) -> bool:
    """Whether `index.knn` is already on, so the caller can skip the outage."""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{index}/_settings")
        r.raise_for_status()
        block = next(iter(r.json().values()), {})
    value = (block.get("settings") or {}).get("index", {}).get("knn")
    # settings come back as strings ("true"), not booleans
    return str(value).lower() == "true"


def enable_knn(spec: ClusterSpec, index: str, timeout: float = 120.0) -> None:
    """Close the index, turn on `index.knn`, reopen it.

    The index is unavailable in between. There is no way around it: the
    setting is static, and the API refuses it on an open index.
    """
    base = f"{spec.base_url()}/{index}"
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{base}/_close")
        r.raise_for_status()
        try:
            r = client.put(f"{base}/_settings", json={"index": {"knn": True}})
            r.raise_for_status()
        finally:
            # reopen even if the setting failed, or a failed call would
            # leave the index closed and every query dead
            client.post(f"{base}/_open").raise_for_status()


def ensure_vector_field(spec: ClusterSpec, index: str, dims: int,
                        field: str = "vec", similarity: str = "cosine",
                        timeout: float = 60.0) -> bool:
    """Make sure `field` exists as a knn_vector. Returns whether the index
    had to be closed and reopened to get there."""
    from .schema import mappings_from_profile
    from .embeddings import vector_profile

    reopened = False
    if not knn_enabled(spec, index):
        enable_knn(spec, index)
        reopened = True

    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{index}/_mapping")
        r.raise_for_status()
        block = next(iter(r.json().values()), {})
        props = (block.get("mappings") or {}).get("properties") or {}
        if field in props:
            # the field is already there, but a mapped dimension cannot be
            # changed — writing the wrong width fails per-document, deep in
            # a bulk response, so say plainly what is wrong up front
            mapped = props[field].get("dimension") or props[field].get("dims")
            if mapped and int(mapped) != int(dims):
                raise RuntimeError(
                    f"“{field}” is already mapped for {mapped}-dimension "
                    f"vectors, but this model produces {dims}. A mapped "
                    f"dimension cannot be changed — embed into a different "
                    f"field, or recreate the index.")
            return reopened     # already mapped; leave its settings alone

        mappings = mappings_from_profile(
            vector_profile(dims, similarity, field), engine=spec.engine)
        r = client.put(f"{spec.base_url()}/{index}/_mapping", json=mappings)
        r.raise_for_status()
    return reopened


def _iter_docs(client: httpx.Client, base: str, index: str,
               text_field: str, batch: int):
    """Stream every doc that has the text field, using the scroll API.

    Yields (ids, texts) per page. The scroll context is released in the
    caller's `finally`; leaking one holds heap on every shard until it
    times out.
    """
    body = {"size": batch, "_source": [text_field],
            "query": {"exists": {"field": text_field}}}
    r = client.post(f"{base}/{index}/_search",
                    params={"scroll": SCROLL_TTL}, json=body)
    r.raise_for_status()
    data = r.json()
    scroll_id = data.get("_scroll_id")
    try:
        while True:
            hits = (data.get("hits") or {}).get("hits") or []
            if not hits:
                return
            yield hits
            if not scroll_id:
                return
            r = client.post(f"{base}/_search/scroll",
                            json={"scroll": SCROLL_TTL, "scroll_id": scroll_id})
            r.raise_for_status()
            data = r.json()
            scroll_id = data.get("_scroll_id") or scroll_id
    finally:
        if scroll_id:
            # best effort: a failed cleanup must not mask a real error
            try:
                client.request("DELETE", f"{base}/_search/scroll",
                               json={"scroll_id": [scroll_id]})
            except httpx.HTTPError:
                pass


def embed_existing_docs(spec: ClusterSpec, index: str, model,
                        text_field: str, vector_field: str = "vec",
                        batch: int = BATCH) -> tuple[int, bool]:
    """Embed `text_field` into `vector_field` for every document.

    Returns (documents updated, whether the index had to be reopened).
    """
    reopened = ensure_vector_field(spec, index, model.dims, vector_field)
    base = spec.base_url()
    done = 0
    with httpx.Client(timeout=120) as client:
        for hits in _iter_docs(client, base, index, text_field, batch):
            texts, ids = [], []
            for h in hits:
                val = (h.get("_source") or {}).get(text_field)
                if isinstance(val, list):
                    val = " ".join(str(v) for v in val)
                if not val:
                    continue
                ids.append(h["_id"])
                texts.append(str(val))
            if not texts:
                continue
            vectors = model.embed(texts)
            # a bulk `update` with a partial doc is the atomic update: it
            # sets the vector and leaves every other field alone
            lines = []
            for doc_id, vec in zip(ids, vectors):
                lines.append({"update": {"_index": index, "_id": doc_id}})
                lines.append({"doc": {vector_field: list(vec)}})
            payload = "\n".join(_dumps(line) for line in lines) + "\n"
            r = client.post(f"{base}/_bulk", content=payload,
                            headers={"Content-Type": "application/x-ndjson"})
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(_first_bulk_error(body))
            done += len(ids)

        # make the vectors both searchable and durable, as a commit would
        client.post(f"{base}/{index}/_refresh").raise_for_status()
        client.post(f"{base}/{index}/_flush").raise_for_status()
    return done, reopened


def _dumps(obj) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"))


def _first_bulk_error(body: dict) -> str:
    """The useful reason is buried per-item; surface the first real one."""
    for item in body.get("items") or []:
        for outcome in item.values():
            err = outcome.get("error")
            if err:
                reason = err.get("reason") or str(err)
                caused = (err.get("caused_by") or {}).get("reason")
                return f"{caused or reason}"
    return "bulk update failed"
