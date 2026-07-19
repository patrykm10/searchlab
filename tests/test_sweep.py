"""Sweep tests: validation, cell expansion, lifecycle ordering, matrix report."""

from __future__ import annotations

import json

import pytest

from solrlab.loadtest import LoadResult, RequestRecord
from solrlab.sweep import cell_name, cells, load_sweep, run_sweep, save_sweep

SWEEP_YAML = """
collection: c
base: {engine: solr, solr_nodes: 1}
matrix:
  heap: ["512m", "1g"]
  gc_tune: ["", "-XX:+UseZGC"]
workload:
  gen: {profile: %s, count: 100, seed: 7}
  load: {rps: 20, duration: 5}
"""


def _cfg(tmp_path):
    profile = tmp_path / "p.yaml"
    profile.write_text("fields:\n  id: {type: id}\n  t: {type: text}\n")
    p = tmp_path / "sweep.yaml"
    p.write_text(SWEEP_YAML % profile)
    return load_sweep(p)


def test_sweep_validation(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg["matrix"]["heap"] == ["512m", "1g"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("collection: c\nmatrix: {rps: [1,2]}\nworkload: {load: {rps: 1}}\n")
    with pytest.raises(SystemExit):  # rps isn't a cluster knob
        load_sweep(bad)
    bad.write_text("collection: c\nmatrix: {heap: []}\nworkload: {load: {rps: 1}}\n")
    with pytest.raises(SystemExit):  # empty list
        load_sweep(bad)


def test_cells_product_and_names(tmp_path):
    cfg = _cfg(tmp_path)
    cs = cells(cfg)
    assert len(cs) == 4
    assert cs[0] == {"heap": "512m", "gc_tune": ""}
    assert cell_name(cs[0]) == "heap=512m, gc_tune=default"
    assert cell_name(cs[3]) == "heap=1g, gc_tune=-XX:+UseZGC"


def _fake_result(p99: float) -> LoadResult:
    r = LoadResult(target_rps=20)
    r.records = [RequestRecord(i * 0.05, p99 - 5 + (i % 3) * 5, 200, "q", True)
                 for i in range(100)]
    r.duration = 5.0
    return r


def test_run_sweep_lifecycle_and_isolation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    calls: list[tuple] = []
    p99_by_heap = {"512m": 200.0, "1g": 40.0}

    class FakeStats:
        docs, started = 100, 0.0

    ops = {
        "up": lambda spec: calls.append(("up", spec.heap, spec.gc_tune)),
        "down": lambda spec: calls.append(("down", spec.heap, spec.gc_tune)),
        "create": lambda spec, coll: calls.append(("create", coll)),
        "index": lambda spec, coll, path, threads: (calls.append(("index",)), FakeStats())[1],
        "load": lambda spec, coll, lc, seed: _fake_result(p99_by_heap[spec.heap]),
    }
    results = run_sweep(cfg, ops=ops, log=lambda *_: None)

    assert len(results) == 4
    # every cell: up -> create -> index -> (load) -> down, strictly per cell
    seq = [c[0] for c in calls]
    assert seq == ["up", "create", "index", "down"] * 4
    # cells carry the right config through to the spec
    ups = [c for c in calls if c[0] == "up"]
    assert ups[0] == ("up", "512m", "") and ups[3] == ("up", "1g", "-XX:+UseZGC")
    # metrics flow through
    assert results[0]["metrics"]["p99_ms"] == pytest.approx(200, abs=6)
    assert results[2]["metrics"]["p99_ms"] == pytest.approx(40, abs=6)


def test_run_sweep_down_even_on_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    calls = []
    ops = {
        "up": lambda spec: calls.append("up"),
        "down": lambda spec: calls.append("down"),
        "create": lambda spec, coll: (_ for _ in ()).throw(RuntimeError("boom")),
        "index": None, "load": None,
    }
    with pytest.raises(RuntimeError):
        run_sweep(cfg, ops=ops, log=lambda *_: None)
    assert calls == ["up", "down"]  # teardown ran despite the failure


def test_save_sweep_matrix_html(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    results = [
        {"cell": {"heap": "512m"}, "name": "heap=512m", "index_docs_per_s": 900.0,
         "metrics": {"requests": 100, "errors": 2, "dropped": 0, "achieved_rps": 19.0,
                     "p50_ms": 30.0, "p90_ms": 90.0, "p99_ms": 200.0, "p999_ms": 210.0}},
        {"cell": {"heap": "1g"}, "name": "heap=1g", "index_docs_per_s": 1500.0,
         "metrics": {"requests": 100, "errors": 0, "dropped": 0, "achieved_rps": 20.0,
                     "p50_ms": 10.0, "p90_ms": 20.0, "p99_ms": 40.0, "p999_ms": 45.0}},
    ]
    json_path, html_path = save_sweep(results, cfg, tmp_path / "out")
    data = json.loads(json_path.read_text())
    assert len(data["results"]) == 2 and "matrix" in data
    html = html_path.read_text()
    assert "heap=512m" in html and "heap=1g" in html
    # best-per-column highlighting: 1g wins p99 (40), 512m must not be marked
    assert html.count("background:#dcefdc") >= 6  # p50/p90/p99/p999/errors/rps/idx for 1g
    row_1g = html.split("heap=1g")[1].split("</tr>")[0]
    assert row_1g.count("background:#dcefdc") >= 6
    row_512 = html.split("heap=512m")[1].split("</tr>")[0]
    # 512m ties on dropped=0 (ties count as best) but wins nothing else
    assert row_512.count("background:#dcefdc") == 1
