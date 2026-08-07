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

    return {"shards": shards, "manage": False, "note": NOTE}
