"""Snapshot the metrics that matter from Solr's /admin/metrics API.

A load test without before/after metrics is half a story. This pulls the
handful of numbers that explain most performance behavior: heap and GC, cache
hit ratios, searcher warmup, update handler activity, and merge counts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .cluster import ClusterSpec


def _get(d: dict, *path: str, default: Any = None) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def snapshot_node_solr(base_url: str) -> dict:
    """Fetch and distill metrics from one Solr node."""
    with httpx.Client(timeout=15) as client:
        r = client.get(
            f"{base_url}/admin/metrics",
            params={"wt": "json", "group": "jvm,core"},
        )
        r.raise_for_status()
        metrics = r.json().get("metrics", {})

    out: dict[str, Any] = {"ts": time.time()}

    jvm = metrics.get("solr.jvm", {})
    heap_used = _get(jvm, "memory.heap.used")
    heap_max = _get(jvm, "memory.heap.max")
    out["jvm"] = {
        "heap_used_mb": round(heap_used / 2**20, 1) if heap_used else None,
        "heap_max_mb": round(heap_max / 2**20, 1) if heap_max else None,
        "gc": {},
    }
    for key, val in jvm.items():
        # gc.G1-Young-Generation.count / .time, gc.ZGC-Pauses.count, etc.
        if key.startswith("gc.") and key.endswith((".count", ".time")):
            collector, stat = key[3:].rsplit(".", 1)
            out["jvm"]["gc"].setdefault(collector, {})[stat] = val

    out["cores"] = {}
    for group, vals in metrics.items():
        if not group.startswith("solr.core."):
            continue
        core = group[len("solr.core."):]
        caches = {}
        for cache in ("queryResultCache", "filterCache", "documentCache"):
            stats = _get(vals, f"CACHE.searcher.{cache}")
            if isinstance(stats, dict):
                caches[cache] = {
                    "hitratio": _get(stats, "hitratio"),
                    "size": _get(stats, "size"),
                    "evictions": _get(stats, "evictions"),
                }
        out["cores"][core] = {
            "num_docs": _get(vals, "SEARCHER.searcher.numDocs"),
            "deleted_docs": _get(vals, "SEARCHER.searcher.deletedDocs"),
            "warmup_ms": _get(vals, "SEARCHER.searcher.warmupTime"),
            # segments rise as documents arrive and fall when merges run —
            # the sawtooth that the merge-policy knobs actually control
            "segments": _get(vals, "INDEX.segments"),
            "size_bytes": _get(vals, "INDEX.sizeInBytes"),
            "lucene": {
                # maxDoc counts deleted docs too, so maxDoc-numDocs is the
                # dead weight still being carried in the index
                "max_doc": _get(vals, "SEARCHER.searcher.maxDoc"),
                "index_version": _get(vals, "SEARCHER.searcher.indexVersion"),
                # a new searcher per commit; errors/maxReached mean commits
                # are arriving faster than searchers can be opened
                "searchers_opened": _get(vals, "SEARCHER.new", "count")
                                    or _get(vals, "SEARCHER.new"),
                "searcher_warmup_ms": _get(vals, "SEARCHER.searcher.warmupTime"),
                "searcher_opened_at": _get(vals, "SEARCHER.searcher.openedAt"),
                # sorts that had to rank the full result set vs ones that
                # could stop early — the cost of sorting deep result sets
                "full_sorts": _get(vals, "SEARCHER.searcher.fullSortCount"),
                "skip_sorts": _get(vals, "SEARCHER.searcher.skipSortCount"),
            },
            "caches": caches,
            "update": {
                "adds_cumulative": _get(vals, "UPDATE.updateHandler.cumulativeAdds", "count"),
                "commits": _get(vals, "UPDATE.updateHandler.commits", "count"),
                "soft_commits": _get(vals, "UPDATE.updateHandler.softAutoCommits"),
                "merges_major": _get(vals, "INDEX.merge.major", "count"),
                "merges_minor": _get(vals, "INDEX.merge.minor", "count"),
            },
            "select_p99_ms": _get(vals, "QUERY./select.requestTimes", "p99_ms"),
            "select_rate_1m": _get(vals, "QUERY./select.requestTimes", "1minRate"),
        }
    return out


def snapshot_cluster(spec: ClusterSpec) -> dict:
    nodes = {}
    eng = spec.eng()
    for i in range(spec.solr_nodes):
        name = f"{eng.node_prefix}{i + 1}"
        try:
            nodes[name] = eng.snapshot_node(spec.base_url(i))
        except httpx.HTTPError as e:
            nodes[name] = {"error": f"{type(e).__name__}: {e}"}
    return nodes


def format_snapshot(nodes: dict) -> str:
    lines = []
    for name, snap in nodes.items():
        if "error" in snap:
            lines.append(f"{name}: UNREACHABLE ({snap['error']})")
            continue
        jvm = snap["jvm"]
        heap = f"{jvm['heap_used_mb']}/{jvm['heap_max_mb']} MB" if jvm["heap_used_mb"] else "?"
        gc_bits = [
            f"{coll}: {s.get('count', '?')} pauses, {s.get('time', '?')} ms total"
            for coll, s in jvm["gc"].items()
        ]
        lines.append(f"{name}  heap {heap}   {'; '.join(gc_bits) or 'no GC data'}")
        for core, c in snap["cores"].items():
            lines.append(
                f"  {core}: {c['num_docs']} docs ({c['deleted_docs']} deleted), "
                f"warmup {c['warmup_ms']} ms"
            )
            for cache, s in c["caches"].items():
                hr = s["hitratio"]
                lines.append(
                    f"    {cache}: hitratio {hr if hr is not None else '?'} "
                    f"size {s['size']} evictions {s['evictions']}"
                )
            u = c["update"]
            lines.append(
                f"    updates: {u['adds_cumulative']} adds, {u['commits']} commits, "
                f"merges {u['merges_minor']}/{u['merges_major']} (minor/major)"
            )
            if c["select_p99_ms"] is not None:
                lines.append(
                    f"    /select: p99 {round(c['select_p99_ms'], 1)} ms, "
                    f"1m rate {round(c['select_rate_1m'] or 0, 1)}/s"
                )
    return "\n".join(lines)


def save_snapshot(nodes: dict, out: str | Path) -> None:
    Path(out).write_text(json.dumps(nodes, indent=2))


# ------------------------------------------------------------------- diff ---

def diff_snapshots(before: dict, after: dict) -> str:
    """Human-readable delta between two saved cluster snapshots — the
    before/after story of a load test: GC time burned, cache hit-ratio
    movement, commits and merges triggered, docs added."""
    lines = []
    for name in after:
        a, b = before.get(name, {}), after[name]
        if "error" in b or "error" in a or not a:
            lines.append(f"{name}: no comparable data")
            continue
        dt = b["ts"] - a["ts"]
        lines.append(f"{name}  (over {dt:.0f}s)")
        for coll, gb in b["jvm"]["gc"].items():
            ga = a["jvm"]["gc"].get(coll, {"count": 0, "time": 0})
            lines.append(
                f"  gc {coll}: +{gb.get('count', 0) - ga.get('count', 0)} pauses, "
                f"+{gb.get('time', 0) - ga.get('time', 0)} ms"
            )
        for core, cb in b["cores"].items():
            ca = a["cores"].get(core)
            if not ca:
                continue
            lines.append(f"  {core}:")
            lines.append(
                f"    docs +{(cb['num_docs'] or 0) - (ca['num_docs'] or 0)}, "
                f"deleted +{(cb['deleted_docs'] or 0) - (ca['deleted_docs'] or 0)}"
            )
            for cache, sb in cb["caches"].items():
                sa = ca["caches"].get(cache, {})
                ha, hb = sa.get("hitratio"), sb.get("hitratio")
                if ha is not None and hb is not None:
                    lines.append(
                        f"    {cache}: hitratio {ha:.3f} -> {hb:.3f} ({hb - ha:+.3f}), "
                        f"evictions +{(sb.get('evictions') or 0) - (sa.get('evictions') or 0)}"
                    )
            ua, ub = ca["update"], cb["update"]
            lines.append(
                f"    updates: +{(ub['adds_cumulative'] or 0) - (ua['adds_cumulative'] or 0)} adds, "
                f"+{(ub['commits'] or 0) - (ua['commits'] or 0)} commits, "
                f"merges +{(ub['merges_minor'] or 0) - (ua['merges_minor'] or 0)}/"
                f"+{(ub['merges_major'] or 0) - (ua['merges_major'] or 0)} (minor/major)"
            )
    return "\n".join(lines)
