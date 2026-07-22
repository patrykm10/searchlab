"""Add embeddings to documents that are already indexed.

Solr can't compute vectors itself, so making an existing collection
searchable by meaning means reading the documents back out, embedding a
text field here, and writing them again. That is also exactly what a real
re-embedding migration looks like — changing model, or changing
dimensions — so it's worth being able to watch it happen and time it.

Documents are streamed with cursorMark rather than deep paging, which
would get quadratically slower the further in it went.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec
from .schema import apply_schema

BATCH = 200


def ensure_vector_field(spec: ClusterSpec, collection: str, dims: int,
                        field: str = "vec", similarity: str = "cosine") -> None:
    """Create the DenseVectorField if the collection hasn't got it yet.

    Reuses the schema machinery so the field is declared exactly the way
    `searchlab schema` would, HNSW knobs and all.
    """
    from .embeddings import vector_profile

    with httpx.Client(timeout=30) as client:
        r = client.get(f"{spec.base_url()}/{collection}/schema/fields/{field}",
                       params={"wt": "json"})
        if r.status_code == 200:
            return          # already there; leave its settings alone
    apply_schema(spec, collection, vector_profile(dims, similarity, field))


def _iter_docs(client: httpx.Client, base: str, collection: str,
               text_field: str, batch: int):
    """Stream every doc that has the text field, using cursorMark."""
    cursor = "*"
    while True:
        r = client.get(f"{base}/{collection}/select", params={
            "q": f"{text_field}:*", "rows": batch, "wt": "json",
            "fl": f"id,{text_field}", "sort": "id asc", "cursorMark": cursor,
        })
        r.raise_for_status()
        data = r.json()
        docs = data.get("response", {}).get("docs", [])
        if docs:
            yield docs
        nxt = data.get("nextCursorMark")
        if not nxt or nxt == cursor or not docs:
            return
        cursor = nxt


def embed_existing_docs(spec: ClusterSpec, collection: str, model,
                        text_field: str, vector_field: str = "vec",
                        batch: int = BATCH) -> int:
    """Embed `text_field` into `vector_field` for every document.

    Returns how many documents were updated.
    """
    ensure_vector_field(spec, collection, model.dims, vector_field)
    base = spec.base_url()
    done = 0
    with httpx.Client(timeout=120) as client:
        for docs in _iter_docs(client, base, collection, text_field, batch):
            texts, ids = [], []
            for d in docs:
                val = d.get(text_field)
                if isinstance(val, list):
                    val = " ".join(str(v) for v in val)
                if not val:
                    continue
                ids.append(d["id"])
                texts.append(str(val))
            if not texts:
                continue
            vectors = model.embed(texts)
            # atomic update: set only the vector, leave the rest untouched
            payload = [{"id": i, vector_field: {"set": v}}
                       for i, v in zip(ids, vectors)]
            r = client.post(f"{base}/{collection}/update",
                            params={"wt": "json"}, json=payload)
            r.raise_for_status()
            done += len(payload)
        client.get(f"{base}/{collection}/update",
                   params={"commit": "true", "wt": "json"})
    return done
