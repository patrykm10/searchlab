"""Shard/replica topology for OpenSearch / Elasticsearch.

The counterpart of cluster.collection_detail, shaped the same so one
dashboard table renders both engines. The concepts line up more closely
than the vocabulary suggests:

    Solr shard          ->  ES/OS shard
    Solr leader         ->  ES/OS primary
    Solr NRT replica    ->  ES/OS replica

with two differences that matter to the UI. First, ES/OS replicas have no
stable names — a shard just has a primary and N copies — so they are named
positionally here. Second, replica placement is not something you do per
shard: you set `number_of_replicas` on the index and the cluster decides
where the copies go. That knob already exists in tuning_os, so this module
reports topology and leaves managing it to the tuning panel.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

# Shard states, translated into the vocabulary the dashboard already colours
# (active / recovering / down). RELOCATING and INITIALIZING are both "this
# copy is not ready yet", which is what "recovering" means to a reader.
_STATES = {
    "STARTED": "active",
    "INITIALIZING": "recovering",
    "RELOCATING": "recovering",
    "UNASSIGNED": "down",
}

NOTE = ("Replica placement is not per-shard here the way it is in Solr: "
        "you set <b>number of replicas</b> on the index and the cluster "
        "decides where the copies live. That knob is in Tuning. An "
        "unassigned copy usually means there is no node left to hold it — "
        "a 3-node cluster cannot place 3 replicas of a shard, because a "
        "copy will not share a node with itself.")


SPLIT_NOTE = ("Splitting works differently here. Solr divides one shard "
              "in place and the collection keeps taking writes. OpenSearch "
              "copies the <i>whole index</i> into a new one with more "
              "shards, and the original has to stop accepting writes "
              "first — so this is a migration, not an adjustment. The "
              "original is left behind, still read-only, so you can check "
              "the new index before pointing anything at it.")


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def index_topology(spec: ClusterSpec, index: str, timeout: float = 15.0) -> dict:
    """Shard/replica layout for one index.

    Returns the same {shards: {name: {state, replicas: {...}}}} shape as
    the Solr side, so the dashboard needs no engine-specific rendering.
    """
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/_cat/shards/{index}",
                       params={"format": "json",
                               "h": "shard,prirep,state,node,docs,store"})
        r.raise_for_status()
        rows = r.json()

    # group the flat _cat rows by shard, primary first so it is named first
    by_shard: dict[str, list[dict]] = {}
    for row in rows:
        by_shard.setdefault(str(row.get("shard")), []).append(row)

    shards: dict = {}
    for sid in sorted(by_shard, key=lambda s: _int(s) if _int(s) is not None else 0):
        copies = sorted(by_shard[sid], key=lambda c: c.get("prirep") != "p")
        replicas = {}
        shard_state = "down"
        n = 0
        for copy in copies:
            primary = copy.get("prirep") == "p"
            state = _STATES.get(copy.get("state"), (copy.get("state") or "").lower())
            if primary:
                shard_state = state
            if primary:
                name = "primary"
            else:
                n += 1
                name = f"replica {n}"
            replicas[name] = {
                "node": copy.get("node") or "unassigned",
                # only the primary carries a segments handle: segments_os
                # reads primaries, so a replica button would show the
                # primary's segments while claiming to be the replica's
                "core": sid if primary else "",
                "type": "primary" if primary else "replica",
                "state": state,
                "leader": primary,
                # _cat reports docs per copy, so the table does not need the
                # Solr metrics snapshot to fill this column
                "docs": _int(copy.get("docs")),
            }
        shards[f"shard {sid}"] = {"state": shard_state, "replicas": replicas}

    current = len(shards)
    return {"shards": shards, "manage": False, "note": NOTE,
            # splitting is offered, but as its own operation with its own
            # explanation — it is not the Solr button under a new label.
            # Only the first few multiples: a lab cluster splitting into 64
            # shards is a menu full of answers nobody wants.
            "split": {"current": current,
                      "targets": split_targets(current)[:5],
                      "note": SPLIT_NOTE}}


def shard_count(spec: ClusterSpec, index: str, timeout: float = 15.0) -> int:
    """How many primary shards the index has now."""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{index}/_settings")
        r.raise_for_status()
        block = next(iter(r.json().values()), {})
    return int((block.get("settings") or {}).get("index", {})
               .get("number_of_shards", 1))


def split_targets(current: int, limit: int = 64) -> list[int]:
    """Shard counts an index of `current` shards can be split into.

    The API requires the source count to be a factor of the target, so
    offering anything else just produces a rejection the user has to read.
    """
    return [n for n in range(current * 2, limit + 1) if n % current == 0]


def split_index(spec: ClusterSpec, index: str, target_shards: int,
                target_name: str | None = None, timeout: float = 900.0) -> dict:
    """Copy `index` into a new index with more shards.

    The source is put into read-only mode first, because the API refuses
    to resize an index that is still taking writes — and it is left that
    way afterwards rather than silently reopened, so the new index can be
    checked before anything is pointed at it.
    """
    current = shard_count(spec, index)
    if target_shards <= current:
        raise ValueError(
            f"“{index}” already has {current} shards; splitting has to "
            f"increase that.")
    if target_shards % current:
        raise ValueError(
            f"{current} shards can only be split into a multiple of "
            f"{current} — {target_shards} is not one. Try "
            f"{', '.join(str(n) for n in split_targets(current)[:4])}.")

    target = target_name or f"{index}_s{target_shards}"
    base = spec.base_url()
    with httpx.Client(timeout=timeout) as client:
        r = client.put(f"{base}/{index}/_settings",
                       json={"index.blocks.write": True})
        r.raise_for_status()
        r = client.post(f"{base}/{index}/_split/{target}",
                        json={"settings": {"index.number_of_shards": target_shards}})
        if r.status_code >= 400:
            # the source is already read-only at this point; say so, or the
            # user is left with an index that quietly stopped taking writes
            raise RuntimeError(
                f"{_reason(r)} “{index}” is read-only now — clear "
                f"index.blocks.write to let it take writes again.")
        body = r.json()

    return {"source": index, "target": target,
            "from_shards": current, "to_shards": target_shards,
            "acknowledged": bool(body.get("acknowledged"))}


def _reason(resp) -> str:
    try:
        err = resp.json().get("error") or {}
        return (err.get("reason") or str(err)).rstrip(".") + "."
    except ValueError:
        return f"Split failed ({resp.status_code})."
