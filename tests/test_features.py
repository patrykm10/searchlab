"""Tests for dashboard, chaos scenarios, and metrics diff."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from solrlab.chaos import load_scenario
from solrlab.cluster import ClusterSpec
from solrlab.dashboard import _DemoState, make_handler
from solrlab.metrics import diff_snapshots

# -------------------------------------------------------------- dashboard ---

def test_demo_snapshot_shape():
    snap = _DemoState(ClusterSpec(solr_nodes=3)).snapshot()
    assert set(snap) == {"ts", "spec", "nodes", "cluster", "loadtest"}
    assert set(snap["nodes"]) == {"solr1", "solr2", "solr3"}
    n = snap["nodes"]["solr1"]
    assert 0 < n["jvm"]["heap_used_mb"] <= n["jvm"]["heap_max_mb"]
    core = next(iter(n["cores"].values()))
    assert core["select_p99_ms"] > 0
    assert 0 <= core["caches"]["queryResultCache"]["hitratio"] <= 1
    assert snap["cluster"]["live_nodes"] == 3


def test_dashboard_serves_page_and_api():
    handler = make_handler(ClusterSpec(), demo=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "CLUSTER RECORDER" in page and "/api/snapshot" in page
        snap = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot").read())
        assert "nodes" in snap and "solr1" in snap["nodes"]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
    finally:
        server.shutdown()


# ------------------------------------------------------------------ chaos ---

def test_scenario_loads_and_sorts(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        "steps:\n"
        "  - {at: 60, action: unpause, node: solr2}\n"
        "  - {at: 10, action: pause, node: solr2}\n"
    )
    steps = load_scenario(p)
    assert [s["at"] for s in steps] == [10, 60]


def test_scenario_rejects_unknown_action(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("steps:\n  - {at: 5, action: explode, node: solr1}\n")
    with pytest.raises(SystemExit):
        load_scenario(p)


def test_scenario_requires_fields(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("steps:\n  - {action: kill}\n")
    with pytest.raises(SystemExit):
        load_scenario(p)


def test_shipped_example_scenario_valid():
    from pathlib import Path
    steps = load_scenario(Path(__file__).parent.parent / "examples" / "chaos-node-loss.yaml")
    assert steps[0]["action"] == "pause"
    assert steps[-1]["action"] == "start"


# ---------------------------------------------------------- metrics diff ---

def _snap(ts, gc_count, gc_time, docs, hitratio, adds, commits, mmin):
    return {
        "solr1": {
            "ts": ts,
            "jvm": {"heap_used_mb": 500, "heap_max_mb": 1024,
                    "gc": {"G1-Young-Generation": {"count": gc_count, "time": gc_time}}},
            "cores": {
                "c1": {
                    "num_docs": docs, "deleted_docs": 0, "warmup_ms": 100,
                    "caches": {"filterCache": {"hitratio": hitratio, "size": 10, "evictions": 0}},
                    "update": {"adds_cumulative": adds, "commits": commits,
                               "soft_commits": 0, "merges_minor": mmin, "merges_major": 0},
                    "select_p99_ms": 20, "select_rate_1m": 5,
                }
            },
        }
    }


def test_metrics_diff_deltas():
    before = _snap(1000, 10, 200, 1000, 0.50, 1000, 2, 1)
    after = _snap(1120, 25, 900, 6000, 0.85, 9000, 8, 4)
    out = diff_snapshots(before, after)
    assert "+15 pauses" in out and "+700 ms" in out
    assert "docs +5000" in out
    assert "0.500 -> 0.850 (+0.350)" in out
    assert "+8000 adds" in out and "+6 commits" in out
    assert "merges +3/+0" in out
    assert "(over 120s)" in out


def test_metrics_diff_handles_unreachable():
    before = {"solr1": {"error": "timeout"}}
    after = _snap(1, 0, 0, 0, 0.1, 0, 0, 0)
    assert "no comparable data" in diff_snapshots(before, after)
