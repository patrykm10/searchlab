"""Per-shard Lucene segment detail for OpenSearch / Elasticsearch.

The counterpart of segments.py. Same purpose — see what the segment count
is actually made of — but the engine reports a different, and arguably
more instructive, fact about each segment.

Solr's `_segments` says where a segment came from (a flush or a merge).
ES/OS instead say what state it is in:

    committed   written to disk, so it survives a restart
    search      visible to queries

Those two are independent, and the combination is the whole "I indexed it,
where is it?" question made concrete: a segment that is searchable but not
committed exists only in memory and the translog; one that is committed but
not searchable is on disk but waiting for a refresh. Rather than inventing
a flush/merge label that the API does not provide, this surfaces the state
the engine actually reports.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

NOTE = ("<b>Committed</b> means the segment is on disk and survives a "
        "restart; <b>searchable</b> means queries can see it. They move "
        "independently — a searchable-but-uncommitted segment lives in "
        "memory and the translog, and a committed-but-unsearchable one is "
        "safe on disk but waiting for the next refresh. Deleted documents "
        "keep costing disk and search time until a merge rewrites the "
        "segment holding them.")


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _state(seg: dict) -> str:
    committed, searchable = seg.get("committed"), seg.get("search")
    if committed and searchable:
        return "committed"
    if searchable:
        return "searchable only"
    if committed:
        return "committed only"
    return "pending"


def index_segments(spec: ClusterSpec, index: str, shard: str | None = None,
                   timeout: float = 30.0) -> dict:
    """Segment detail for an index, optionally narrowed to one shard.

    ES/OS have no per-replica core name the way Solr does, so the unit here
    is the shard; `shard` is a string to match the API's own key type.
    """
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{index}/_segments")
        r.raise_for_status()
        data = r.json()

    block = (data.get("indices") or {}).get(index)
    if block is None:
        block = next(iter((data.get("indices") or {}).values()), {})

    segments = []
    lucene = None
    for shard_id, replicas in (block.get("shards") or {}).items():
        if shard is not None and str(shard_id) != str(shard):
            continue
        for replica in replicas:
            # only the primary, or every replica reports the same segments
            # twice and the totals double
            if not (replica.get("routing") or {}).get("primary", True):
                continue
            for name, seg in (replica.get("segments") or {}).items():
                live = seg.get("num_docs") or 0
                deleted = seg.get("deleted_docs") or 0
                total = live + deleted
                lucene = lucene or seg.get("version")
                segments.append({
                    "name": f"{name} (shard {shard_id})",
                    "docs": live,
                    "deleted": deleted,
                    "deleted_pct": round(deleted / total * 100, 1) if total else 0.0,
                    "bytes": seg.get("size_in_bytes") or 0,
                    "size": _fmt_bytes(seg.get("size_in_bytes")),
                    "source": _state(seg),
                    "age": None,          # ES/OS do not report a write time
                    "version": seg.get("version"),
                })
    segments.sort(key=lambda s: -s["bytes"])

    total_docs = sum(s["docs"] for s in segments)
    total_deleted = sum(s["deleted"] for s in segments)
    total_bytes = sum(s["bytes"] for s in segments)
    by_source: dict[str, int] = {}
    for s in segments:
        by_source[s["source"]] = by_source.get(s["source"], 0) + 1

    return {
        "core": index if shard is None else f"{index} shard {shard}",
        "segments": segments,
        "note": NOTE,
        "summary": {
            "count": len(segments),
            "docs": total_docs,
            "deleted": total_deleted,
            "deleted_pct": round(total_deleted / (total_docs + total_deleted) * 100, 1)
                           if (total_docs + total_deleted) else 0.0,
            "bytes": total_bytes,
            "size": _fmt_bytes(total_bytes),
            "largest": segments[0]["size"] if segments else "—",
            "by_source": by_source,
            "lucene": lucene,
        },
    }
