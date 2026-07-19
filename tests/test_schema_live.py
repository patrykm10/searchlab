"""Tests for schema derivation, live load streaming, and histogram output."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from click.testing import CliRunner

from solrlab.cli import main
from solrlab.cluster import ClusterSpec
from solrlab.dashboard import _DemoState
from solrlab.loadtest import RequestRecord, histogram, run_load, save_report
from solrlab.schema import apply_schema, fields_from_profile

# ----------------------------------------------------------------- schema ---

def test_schema_field_mapping():
    profile = {"fields": {
        "id": {"type": "id"},
        "body_t": {"type": "text"},
        "cat_s": {"type": "categorical", "cardinality": 5},
        "price_f": {"type": "float"},
        "n_i": {"type": "int"},
        "ts_dt": {"type": "date"},
        "ok_b": {"type": "bool"},
        "tags_ss": {"type": "multivalued", "of": {"type": "categorical", "cardinality": 3}},
        "uid_s": {"type": "keyword", "solr": {"docValues": False}},
    }}
    defs = {f["name"]: f for f in fields_from_profile(profile)}
    assert "id" not in defs  # uniqueKey untouched
    assert defs["body_t"]["type"] == "text_general" and "docValues" not in defs["body_t"]
    assert defs["cat_s"]["type"] == "string" and defs["cat_s"]["docValues"] is True
    assert defs["price_f"]["type"] == "pfloat"
    assert defs["n_i"]["type"] == "pint"
    assert defs["ts_dt"]["type"] == "pdate"
    assert defs["ok_b"]["type"] == "boolean"
    assert defs["tags_ss"]["multiValued"] is True and defs["tags_ss"]["type"] == "string"
    assert defs["uid_s"]["docValues"] is False  # solr: override wins


def test_schema_dry_run_needs_no_cluster():
    out = apply_schema(None, "x", {"fields": {"a_t": {"type": "text"}}}, dry_run=True)
    assert "would apply" in out and "text_general" in out


def test_schema_unknown_type_exits():
    with pytest.raises(SystemExit):
        fields_from_profile({"fields": {"x": {"type": "geo"}}})


# ------------------------------------------------------------ live stream ---

@pytest.fixture
async def mock_solr(aiohttp_server):
    async def select(request):
        await asyncio.sleep(0.005)
        return web.json_response({"response": {}})
    app = web.Application()
    app.router.add_get("/solr/test/select", select)
    return await aiohttp_server(app)


async def test_load_writes_live_file(mock_solr, tmp_path):
    live = tmp_path / "live.json"
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    await run_load(base, "test", rps=40, duration=2.5, seed=1, live_file=live)
    data = json.loads(live.read_text())
    assert data["target_rps"] == 40
    assert data["recent_rps"] > 20
    assert data["recent_p99_ms"] > 0
    assert data["duration_s"] == 2.5


def test_demo_snapshot_loadtest_lifecycle():
    demo = _DemoState(ClusterSpec())
    demo.t0 -= 60  # jump into the demo test window (starts at t=20)
    snap = demo.snapshot()
    lt = snap["loadtest"]
    assert lt is not None and lt["target_rps"] == 50 and lt["recent_p99_ms"] > 0
    demo.t0 += 60  # back to t≈0: before the window, no test
    assert demo.snapshot()["loadtest"] is None


# -------------------------------------------------------------- histogram ---

def test_histogram_buckets():
    recs = [RequestRecord(0, ms, 200, "q", True) for ms in [0.5, 3, 3, 15, 80, 450, 12000]]
    hist = histogram(recs)
    as_map = {(b["gt_ms"], b["le_ms"]): b["count"] for b in hist}
    assert as_map[(0, 1)] == 1
    assert as_map[(2, 5)] == 2
    assert as_map[(10, 20)] == 1
    assert as_map[(50, 100)] == 1
    assert as_map[(200, 500)] == 1
    assert as_map[(10000, None)] == 1
    assert sum(b["count"] for b in hist) == 7


async def test_report_includes_p999_and_histogram(mock_solr, tmp_path):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    out = tmp_path / "r.json"
    save_report(await run_load(base, "test", rps=50, duration=2, seed=1), out)
    data = json.loads(out.read_text())
    assert "p999_ms" in data and data["p999_ms"] >= data["p99_ms"]
    assert data["histogram"] and sum(b["count"] for b in data["histogram"]) == data["requests"]


# -------------------------------------------------------------------- cli ---

def test_new_commands_registered():
    r = CliRunner().invoke(main, ["--help"])
    for cmd in ("schema", "quickstart", "dashboard", "metrics-diff"):
        assert cmd in r.output
    r = CliRunner().invoke(main, ["schema", "--help"])
    assert "--dry-run" in r.output
