"""Control-panel plumbing: LoadControl steering, commit/optimize helpers,
the log ring buffer, and the dashboard handler's routing — all docker-free.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from aiohttp import web

from searchlab.cluster import (
    ClusterSpec,
    commit,
    delete_all_docs,
    expunge_deletes,
    optimize,
    reload_collection,
)
from searchlab.dashboard import make_handler
from searchlab.loadtest import LoadControl, run_load
from searchlab.logstream import LogStream


# ------------------------------------------------------------ LoadControl ---

@pytest.fixture
async def mock_solr(aiohttp_server):
    async def select(request):
        await asyncio.sleep(0.005)
        return web.json_response({"response": {"numFound": 1, "docs": []}})

    app = web.Application()
    app.router.add_get("/solr/test/select", select)
    return await aiohttp_server(app)


async def test_control_stop_ends_run_early(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    control = LoadControl(rps=50)

    async def stop_soon():
        await asyncio.sleep(1.0)
        control.stop_requested = True

    task = asyncio.create_task(stop_soon())
    result = await run_load(base, "test", rps=50, duration=60, seed=1, control=control)
    await task
    assert result.duration < 5  # nowhere near the 60s duration


async def test_control_rps_change_mid_run(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    control = LoadControl(rps=100)

    async def throttle_soon():
        await asyncio.sleep(2.0)
        control.rps = 10

    task = asyncio.create_task(throttle_soon())
    result = await run_load(base, "test", rps=100, duration=4, seed=1, control=control)
    await task
    first = sum(1 for r in result.records if r.scheduled < 2.0)
    second = sum(1 for r in result.records if r.scheduled >= 2.0)
    assert first > 150   # ~200 expected at 100 rps
    assert second < 60   # ~20 expected at 10 rps
    assert result.target_rps == 10  # tracks the final control value


async def test_control_none_is_backward_compatible(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await run_load(base, "test", rps=50, duration=2, seed=1)
    achieved = len(result.records) / result.duration
    assert abs(achieved - 50) / 50 < 0.1


# --------------------------------------------------------- commit/optimize ---

@pytest.fixture
async def mock_update(aiohttp_server):
    seen = {}

    async def update(request):
        seen.update(request.rel_url.query)
        return web.json_response({"responseHeader": {"status": 0}})

    async def boom(request):
        return web.json_response({}, status=500)

    app = web.Application()
    app.router.add_get("/solr/products/update", update)
    app.router.add_get("/solr/broken/update", boom)
    server = await aiohttp_server(app)
    server.seen = seen
    return server


async def test_commit_sends_commit_param(mock_update):
    spec = ClusterSpec(base_port=mock_update.port)
    out = await asyncio.to_thread(commit, spec, "products")
    assert out["responseHeader"]["status"] == 0
    assert mock_update.seen.get("commit") == "true"


async def test_optimize_sends_max_segments(mock_update):
    spec = ClusterSpec(base_port=mock_update.port)
    out = await asyncio.to_thread(optimize, spec, "products", max_segments=2)
    assert out["responseHeader"]["status"] == 0
    assert mock_update.seen.get("optimize") == "true"
    assert mock_update.seen.get("maxSegments") == "2"


async def test_expunge_asks_for_expunge_not_a_full_optimize(mock_update):
    """Expunge must not send optimize=true — the whole point is that it only
    rewrites segments carrying deletions."""
    spec = ClusterSpec(base_port=mock_update.port)
    await asyncio.to_thread(expunge_deletes, spec, "products")
    assert mock_update.seen.get("expungeDeletes") == "true"
    assert mock_update.seen.get("commit") == "true"
    assert "optimize" not in mock_update.seen


async def test_delete_all_docs_posts_delete_by_query(aiohttp_server):
    seen = {}

    async def update(request):
        seen["params"] = dict(request.rel_url.query)
        seen["body"] = await request.json()
        return web.json_response({"responseHeader": {"status": 0}})

    app = web.Application()
    app.router.add_post("/solr/products/update", update)
    server = await aiohttp_server(app)
    spec = ClusterSpec(base_port=server.port)
    await asyncio.to_thread(delete_all_docs, spec, "products")
    assert seen["body"] == {"delete": {"query": "*:*"}}
    assert seen["params"].get("commit") == "true"   # visible immediately


async def test_reload_raises_on_reported_failure(aiohttp_server):
    """RELOAD can answer 200 while reporting per-node failures."""
    async def admin(request):
        return web.json_response({"failure": {"solr1": "config broken"}})

    app = web.Application()
    app.router.add_get("/solr/admin/collections", admin)
    server = await aiohttp_server(app)
    spec = ClusterSpec(base_port=server.port)
    with pytest.raises(RuntimeError, match="config broken"):
        await asyncio.to_thread(reload_collection, spec, "products")


def test_maintenance_jobs_are_serialised():
    """Expunging while a merge rewrites the same segments only slows both."""
    from searchlab.actions import ActionRunner

    runner = ActionRunner(ClusterSpec())
    runner._maint_busy = True
    for call in (runner.optimize, runner.expunge_deletes,
                 runner.reload_collection, runner.delete_all_docs):
        out = call("products")
        assert out["ok"] is False
        assert "maintenance job is running" in out["error"]


def test_purge_refuses_while_load_test_hits_that_collection():
    from searchlab.actions import ActionRunner
    from searchlab.loadtest import LoadControl

    runner = ActionRunner(ClusterSpec())
    runner._control = LoadControl(rps=10)
    runner._load_meta = {"collection": "products", "started": 0}

    class _Busy:
        def done(self):
            return False
    runner._load_future = _Busy()

    assert "Stop the load test" in runner.delete_all_docs("products")["error"]
    # a different collection is unaffected
    assert runner.delete_all_docs("other")["ok"] is True


async def test_commit_raises_on_500(mock_update):
    import httpx

    spec = ClusterSpec(base_port=mock_update.port)
    with pytest.raises(httpx.HTTPError):
        await asyncio.to_thread(commit, spec, "broken")


# --------------------------------------------------------------- LogStream ---

def test_logstream_since_semantics():
    ls = LogStream(compose_file=Path("/dev/null"), maxlen=5)
    for i in range(3):
        ls._append(f"line {i}\n")
    latest, lines = ls.since(0)
    assert latest == 3
    assert [l for _, l, _ in lines] == ["line 0", "line 1", "line 2"]  # stripped

    latest, lines = ls.since(2)
    assert [l for _, l, _ in lines] == ["line 2"]

    latest, lines = ls.since(99)
    assert lines == []


def test_logstream_ring_buffer_trims():
    ls = LogStream(compose_file=Path("/dev/null"), maxlen=5)
    for i in range(12):
        ls._append(f"line {i}")
    latest, lines = ls.since(0)
    assert latest == 12
    assert len(lines) == 5
    assert lines[0][1] == "line 7"  # oldest retained


def test_logstream_classifies_lines():
    ls = LogStream(compose_file=Path("/dev/null"), maxlen=5)
    ls._append("solr1 | INFO o.a.s.u.DirectUpdateHandler2 start commit{flags=0,openSearcher=true}")
    ls._append("solr1 | INFO something entirely unremarkable")
    _, lines = ls.since(0)
    assert lines[0][2]["tag"] == "commit"
    assert "searchable" in lines[0][2]["desc"]
    assert lines[1][2] is None


# ----------------------------------------------------------- handler smoke ---

@pytest.fixture
def demo_server():
    handler = make_handler(ClusterSpec(), demo=True, runner=None, logs=None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_snapshot_reports_demo_mode(demo_server):
    status, snap = _get(demo_server + "/api/snapshot")
    assert status == 200
    assert snap["actions"] == {"demo": True}
    assert "nodes" in snap


def test_logs_endpoint_empty_without_stream(demo_server):
    status, out = _get(demo_server + "/api/logs?since=0")
    assert status == 200
    assert out == {"latest": 0, "lines": [], "error": None}


def test_post_rejected_in_demo_mode(demo_server):
    status, out = _post(demo_server + "/api/load/start",
                        {"collection": "x", "rps": 10})
    assert status == 409
    assert out["ok"] is False
    assert "demo" in out["error"].lower()


def test_post_unknown_path_and_bad_body(demo_server):
    status, out = _post(demo_server + "/api/nonsense", {})
    assert status == 409
    assert out["ok"] is False

    req = urllib.request.Request(demo_server + "/api/commit", data=b"not json",
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400
