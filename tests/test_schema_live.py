"""Tests for schema derivation, live load streaming, and histogram output."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp import web
from click.testing import CliRunner

from searchlab.cli import main
from searchlab.cluster import ClusterSpec
from searchlab.dashboard import _DemoState
from searchlab.loadtest import RequestRecord, histogram, run_load, save_report
from searchlab.schema import apply_schema, fields_from_profile

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
    # The demo starts pre-warmed (t0 backdated by WINDOW_S), so a test is already
    # running on the very first frame rather than after a cold twenty seconds.
    demo = _DemoState(ClusterSpec())
    lt = demo.snapshot()["loadtest"]
    assert lt is not None and lt["target_rps"] == 50 and lt["recent_p99_ms"] > 0

    demo.t0 = time.time()            # t≈0: before the window opens
    assert demo.snapshot()["loadtest"] is None

    demo.t0 = time.time() - 700      # t≈700: after it closes
    assert demo.snapshot()["loadtest"] is None


def test_demo_history_backfills_one_window():
    demo = _DemoState(ClusterSpec(solr_nodes=2))
    assert "history" not in demo.snapshot()          # only when asked for
    rows = demo.snapshot(with_history=True)["history"]

    assert len(rows) == demo.WINDOW_S // demo.STEP_S + 1
    assert rows[-1]["t"] - rows[0]["t"] == pytest.approx(demo.WINDOW_S, abs=1)
    assert set(rows[0]["p99"]) == {"solr1", "solr2"} == set(rows[0]["heap"])
    # the backfill must carry the same shapes the live snapshot draws
    assert max(max(r["p99"].values()) for r in rows) > 200    # spikes are present
    heaps = [r["heap"]["solr1"] for r in rows]
    assert min(heaps) < 200 and max(heaps) > 700              # a full sawtooth


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
