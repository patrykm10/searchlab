"""Plain-language health findings computed from dashboard snapshots.

Each rule turns a raw signal (heap %, commit rate, achieved vs target RPS)
into a finding a non-expert can act on: what's happening, why it matters,
what to do. The engine keeps the previous snapshot so it can reason about
rates (commits/minute, merge activity) that a single snapshot can't show.

Severity levels: "crit" (something is broken or about to break),
"warn" (degraded, worth attention), "info" (notable but normal).
"""

from __future__ import annotations

import time

HEAP_WARN = 0.80
HEAP_CRIT = 0.92
LOAD_P99_WARN_MS = 100.0
LOAD_P99_CRIT_MS = 500.0
SATURATION_RATIO = 0.90         # achieved / target below this = can't keep up
COMMIT_STORM_PER_MIN = 6.0
CACHE_HITRATIO_LOW = 0.20
CACHE_MIN_LOOKUPS_SIZE = 100    # ignore tiny caches; hitratio is noise there
DELETED_DOCS_RATIO = 0.25
GC_TIME_PCT_WARN = 10.0         # % of wall time spent in GC since last poll


def _f(severity: str, title: str, detail: str) -> dict:
    return {"severity": severity, "title": title, "detail": detail}


class InsightsEngine:
    def __init__(self):
        self._prev: dict | None = None
        self._prev_ts: float = 0.0

    def analyze(self, snap: dict) -> list[dict]:
        findings: list[dict] = []
        nodes = snap.get("nodes") or {}
        cluster = snap.get("cluster") or {}
        lt = snap.get("loadtest")

        findings += self._node_findings(nodes)
        findings += self._cluster_findings(cluster, snap.get("spec") or {})
        if lt:
            findings += self._loadtest_findings(lt)

        self._prev = nodes
        self._prev_ts = snap.get("ts") or time.time()
        order = {"crit": 0, "warn": 1, "info": 2}
        findings.sort(key=lambda f: order.get(f["severity"], 3))
        return findings

    # ------------------------------------------------------------- nodes ---

    def _node_findings(self, nodes: dict) -> list[dict]:
        out: list[dict] = []
        for name, n in nodes.items():
            if n.get("error"):
                out.append(_f("crit", f"{name} is not responding",
                              "The node can't be reached. Queries it was serving are "
                              "failing over to other nodes; capacity is reduced. If this "
                              "wasn't a planned test, check whether the container is running."))
                continue

            jvm = n.get("jvm") or {}
            used, cap = jvm.get("heap_used_mb"), jvm.get("heap_max_mb")
            if used and cap:
                pct = used / cap
                if pct >= HEAP_CRIT:
                    out.append(_f("crit", f"{name} memory is critically full ({pct:.0%})",
                                  "The Java heap is nearly exhausted. Expect long garbage-"
                                  "collection pauses (the node freezes for seconds) and a real "
                                  "risk of crashing out of memory. Reduce load now, or restart "
                                  "with a bigger heap."))
                elif pct >= HEAP_WARN:
                    out.append(_f("warn", f"{name} memory is running high ({pct:.0%})",
                                  "Above ~80% heap, the JVM spends more and more time on "
                                  "garbage collection instead of useful work — queries get "
                                  "slower before anything visibly breaks. Consider lowering "
                                  "the query rate or giving the node more memory."))

            out += self._gc_findings(name, n)
            out += self._core_findings(name, n)
        return out

    def _gc_findings(self, name: str, n: dict) -> list[dict]:
        """GC time delta vs wall time since the previous snapshot."""
        if not self._prev or name not in self._prev:
            return []
        prev_n = self._prev.get(name) or {}
        if prev_n.get("error"):
            return []
        try:
            cur = sum(g.get("time", 0) for g in n["jvm"]["gc"].values())
            old = sum(g.get("time", 0) for g in prev_n["jvm"]["gc"].values())
        except (KeyError, TypeError, AttributeError):
            return []
        wall_ms = (n.get("ts", 0) - prev_n.get("ts", 0)) * 1000
        if wall_ms <= 0:
            return []
        pct = (cur - old) / wall_ms * 100
        if pct >= GC_TIME_PCT_WARN:
            return [_f("warn", f"{name} is spending {pct:.0f}% of its time on garbage collection",
                       "That time is stolen directly from serving queries — users see it as "
                       "slow or uneven response times. Usually a sign the heap is too small "
                       "for the workload; more memory or less load fixes it.")]
        return []

    def _core_findings(self, name: str, n: dict) -> list[dict]:
        out: list[dict] = []
        for core_name, c in (n.get("cores") or {}).items():
            num, deleted = c.get("num_docs") or 0, c.get("deleted_docs") or 0
            total = num + deleted
            if total > 1000 and deleted / total >= DELETED_DOCS_RATIO:
                out.append(_f("info", f"{core_name} carries {deleted / total:.0%} deleted documents",
                              "Updated and deleted documents stay on disk until segments merge, "
                              "taking space and slowing searches slightly. “Merge segments” "
                              "in Maintenance reclaims this."))
            for cache_name, s in (c.get("caches") or {}).items():
                hr, size = s.get("hitratio"), s.get("size") or 0
                if hr is not None and hr < CACHE_HITRATIO_LOW and size >= CACHE_MIN_LOOKUPS_SIZE:
                    out.append(_f("warn", f"{name}: {cache_name} rarely helps ({hr:.0%} hit rate)",
                                  "Almost every query is computed from scratch instead of reusing "
                                  "recent results — queries cost more than they should. Typical "
                                  "causes: every query is unique, or constant indexing keeps "
                                  "invalidating the cache."))
                    break  # one cache finding per node is enough signal
        # commit-storm detection from the update counters
        out += self._commit_findings(name, n)
        return out

    def _commit_findings(self, name: str, n: dict) -> list[dict]:
        if not self._prev or name not in self._prev:
            return []
        prev_n = self._prev.get(name) or {}
        if prev_n.get("error"):
            return []
        try:
            cur = sum((c.get("update") or {}).get("commits", 0) for c in n["cores"].values())
            old = sum((c.get("update") or {}).get("commits", 0) for c in prev_n["cores"].values())
        except (KeyError, TypeError, AttributeError):
            return []
        wall_min = (n.get("ts", 0) - prev_n.get("ts", 0)) / 60
        if wall_min <= 0:
            return []
        per_min = (cur - old) / wall_min
        if per_min >= COMMIT_STORM_PER_MIN:
            return [_f("warn", f"{name} is committing very often (~{per_min:.0f}/min)",
                       "Every commit throws away caches and forces extra disk work, so both "
                       "indexing and queries slow down. Commit less often — for steady "
                       "indexing, once every 10–30 seconds is plenty.")]
        return []

    # ----------------------------------------------------------- cluster ---

    def _cluster_findings(self, cluster: dict, spec: dict) -> list[dict]:
        out: list[dict] = []
        live, expected = cluster.get("live_nodes"), spec.get("solr_nodes")
        if live is not None and expected and live < expected:
            out.append(_f("crit", f"Only {live} of {expected} nodes are alive",
                          "Part of the cluster is down. Remaining nodes carry the full load "
                          "and some data may have no backup copy right now."))
        for name, c in (cluster.get("collections") or {}).items():
            health = c.get("health")
            if health == "RED":
                out.append(_f("crit", f"Collection “{name}” is missing data (RED)",
                              "At least one shard has no working copy at all — some search "
                              "results are silently incomplete until a node comes back."))
            elif health == "YELLOW":
                out.append(_f("warn", f"Collection “{name}” has reduced redundancy (YELLOW)",
                              "All data is still searchable, but some of it exists in only one "
                              "copy — one more failure would lose access to it."))
        return out

    # ---------------------------------------------------------- load test ---

    def _loadtest_findings(self, lt: dict) -> list[dict]:
        out: list[dict] = []
        target, achieved = lt.get("target_rps"), lt.get("recent_rps")
        if target and achieved is not None and lt.get("elapsed_s", 0) > 10:
            if achieved < target * SATURATION_RATIO:
                out.append(_f("warn",
                              f"The cluster can't keep up ({achieved:.0f} of {target:.0f} req/s)",
                              "Requests are being sent faster than the cluster answers them. "
                              "This is the point where real users would see timeouts. Either "
                              "this rate is simply too high, or something above (memory, GC, "
                              "commits) is stealing capacity."))
        p99 = lt.get("recent_p99_ms")
        if p99 is not None:
            if p99 >= LOAD_P99_CRIT_MS:
                out.append(_f("crit", f"Queries are very slow (worst 1% take {p99:.0f} ms)",
                              "Half a second or more per search is territory where users give "
                              "up. Check the findings above — memory pressure, garbage "
                              "collection, or commit churn are the usual culprits."))
            elif p99 >= LOAD_P99_WARN_MS:
                out.append(_f("warn", f"Some queries are getting slow (worst 1% take {p99:.0f} ms)",
                              "Most searches are fine, but the slowest ones now take a "
                              "noticeable fraction of a second. Often the first visible sign "
                              "of memory or cache trouble — worth watching."))
        if lt.get("dropped"):
            out.append(_f("crit", f"{lt['dropped']} requests were dropped",
                          "The load generator gave up on sending some requests because too "
                          "many were already waiting — the cluster is saturated well past "
                          "its capacity at this rate."))
        if lt.get("errors"):
            out.append(_f("warn", f"{lt['errors']} queries failed",
                          "Some requests returned errors instead of results. Check the "
                          "activity log below for the reason — during chaos testing this "
                          "is expected; otherwise it isn't."))
        return out
