"""Per-replica Lucene segment detail.

The dashboard charts a segment *count* per shard, which shows the sawtooth
but not what the segments actually are. This is the level below that: the
individual segments of one replica, their sizes, how many deleted
documents each is carrying, and where each came from — a flush (written
directly by indexing) or a merge (assembled from smaller ones).

That provenance is the interesting part. A big segment sourced from a
merge is the merge policy doing its job; a pile of small flush segments
that never get merged is the merge policy falling behind. Deleted
documents matter because they still cost disk and search time until a
merge rewrites the segment that holds them.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def replica_segments(spec: ClusterSpec, core: str, node: int = 0,
                     timeout: float = 30.0) -> dict:
    """Segment detail for one replica (a Solr core).

    Cores live on a specific node, so the caller passes which one — asking
    the wrong node returns a 404 rather than someone else's segments.
    """
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url(node)}/{core}/admin/segments",
                       params={"wt": "json"})
        r.raise_for_status()
        data = r.json()

    info = data.get("info") or {}
    raw = data.get("segments") or {}
    segments = []
    for name, s in raw.items():
        live = s.get("size") or 0
        deleted = s.get("delCount") or 0
        total = live + deleted
        segments.append({
            "name": name,
            "docs": live,
            "deleted": deleted,
            "deleted_pct": round(deleted / total * 100, 1) if total else 0.0,
            "bytes": s.get("sizeInBytes") or 0,
            "size": _fmt_bytes(s.get("sizeInBytes")),
            # "flush" = written straight from indexing; "merge" = assembled
            # from smaller segments. Which one dominates tells you whether
            # merging is keeping up.
            "source": (s.get("diagnostics") or {}).get("source")
                      or s.get("source") or "?",
            "age": s.get("age"),
            "version": s.get("version"),
        })
    segments.sort(key=lambda s: -s["bytes"])

    total_docs = sum(s["docs"] for s in segments)
    total_deleted = sum(s["deleted"] for s in segments)
    total_bytes = sum(s["bytes"] for s in segments)
    by_source: dict[str, int] = {}
    for s in segments:
        by_source[s["source"]] = by_source.get(s["source"], 0) + 1

    return {
        "core": core,
        "segments": segments,
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
            "lucene": info.get("commitLuceneVersion"),
        },
    }
