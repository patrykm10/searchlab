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

from searchlab.cluster import ClusterSpec, commit, optimize
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
    assert [l for _, l in lines] == ["line 0", "line 1", "line 2"]  # stripped

    latest, lines = ls.since(2)
    assert [l for _, l in lines] == ["line 2"]

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
