"""Health-insight rules: each turns a raw signal into a plain-language finding."""

from __future__ import annotations

from searchlab.insights import InsightsEngine


def _node(heap_used=200, heap_max=1024, ts=1000.0, gc_time=100, commits=2,
          num_docs=10_000, deleted_docs=0, hitratio=0.9, cache_size=500):
    return {
        "ts": ts,
        "jvm": {"heap_used_mb": heap_used, "heap_max_mb": heap_max,
                "gc": {"G1-Young-Generation": {"count": 5, "time": gc_time}}},
        "cores": {
            "products_shard1_replica_n1": {
                "num_docs": num_docs, "deleted_docs": deleted_docs,
                "caches": {"queryResultCache": {"hitratio": hitratio, "size": cache_size,
                                                "evictions": 0}},
                "update": {"adds_cumulative": 100, "commits": commits,
                           "soft_commits": 0, "merges_minor": 0, "merges_major": 0},
                "select_p99_ms": 20, "select_rate_1m": 5,
            }
        },
    }


def _snap(node=None, ts=1000.0, live_nodes=2, solr_nodes=2, health="GREEN", loadtest=None):
    return {
        "ts": ts,
        "spec": {"solr_nodes": solr_nodes},
        "nodes": {"solr1": node if node is not None else _node(ts=ts)},
        "cluster": {"live_nodes": live_nodes,
                    "collections": {"products": {"shards": 2, "health": health}}},
        "loadtest": loadtest,
    }


def _titles(findings):
    return " | ".join(f["title"] for f in findings)


def test_all_clear():
    assert InsightsEngine().analyze(_snap()) == []


def test_heap_warn_and_crit():
    warn = InsightsEngine().analyze(_snap(_node(heap_used=850)))
    assert any(f["severity"] == "warn" and "memory" in f["title"] for f in warn)
    crit = InsightsEngine().analyze(_snap(_node(heap_used=990)))
    assert any(f["severity"] == "crit" and "critically full" in f["title"] for f in crit)


def test_unreachable_node():
    out = InsightsEngine().analyze(_snap({"error": "timeout"}))
    assert any(f["severity"] == "crit" and "not responding" in f["title"] for f in out)


def test_missing_live_nodes_and_health():
    out = InsightsEngine().analyze(_snap(live_nodes=1, health="YELLOW"))
    assert any("1 of 2 nodes" in f["title"] for f in out)
    assert any("YELLOW" in f["title"] for f in out)
    red = InsightsEngine().analyze(_snap(health="RED"))
    assert any(f["severity"] == "crit" and "RED" in f["title"] for f in red)


def test_gc_time_delta_needs_two_snapshots():
    eng = InsightsEngine()
    assert eng.analyze(_snap(_node(ts=1000, gc_time=0), ts=1000)) == []
    # 3000ms GC burned over 10s wall = 30%
    out = eng.analyze(_snap(_node(ts=1010, gc_time=3000), ts=1010))
    assert any("garbage collection" in f["title"] for f in out)


def test_commit_storm_from_delta():
    eng = InsightsEngine()
    eng.analyze(_snap(_node(ts=1000, commits=0), ts=1000))
    # 10 commits in 60s = 10/min
    out = eng.analyze(_snap(_node(ts=1060, commits=10), ts=1060))
    assert any("committing very often" in f["title"] for f in out)


def test_deleted_docs_ratio():
    out = InsightsEngine().analyze(_snap(_node(num_docs=6000, deleted_docs=4000)))
    assert any("deleted documents" in f["title"] for f in out)


def test_low_cache_hitratio():
    out = InsightsEngine().analyze(_snap(_node(hitratio=0.05)))
    assert any("rarely helps" in f["title"] for f in out)
    # tiny caches are ignored
    quiet = InsightsEngine().analyze(_snap(_node(hitratio=0.05, cache_size=3)))
    assert not any("rarely helps" in f["title"] for f in quiet)


def test_loadtest_saturation_p99_dropped_errors():
    lt = {"elapsed_s": 30, "target_rps": 100, "recent_rps": 60,
          "recent_p99_ms": 600, "dropped": 5, "errors": 2}
    out = InsightsEngine().analyze(_snap(loadtest=lt))
    t = _titles(out)
    assert "can't keep up" in t
    assert "very slow" in t
    assert "dropped" in t
    assert "failed" in t
    # crit findings sort first
    assert out[0]["severity"] == "crit"


def test_loadtest_warmup_grace_period():
    lt = {"elapsed_s": 5, "target_rps": 100, "recent_rps": 20, "recent_p99_ms": 50}
    out = InsightsEngine().analyze(_snap(loadtest=lt))
    assert not any("keep up" in f["title"] for f in out)
