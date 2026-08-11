"""Segment counts come through the metrics snapshot."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.metrics import snapshot_node_solr


@pytest.fixture
async def mock_metrics(aiohttp_server):
    async def metrics(request):
        return web.json_response({"metrics": {
            "solr.jvm": {"memory.heap.used": 300 * 2**20,
                         "memory.heap.max": 1024 * 2**20},
            "solr.core.products.shard1.replica_n2": {
                "SEARCHER.searcher.numDocs": 1240,
                "SEARCHER.searcher.deletedDocs": 12,
                "INDEX.segments": 7,
                "INDEX.sizeInBytes": 1200577,
            },
        }})

    app = web.Application()
    app.router.add_get("/solr/admin/metrics", metrics)
    return await aiohttp_server(app)


async def test_snapshot_carries_segment_count_and_size(mock_metrics):
    """Segments come from INDEX.segments, already in the metrics response the
    dashboard polls — no extra round trip needed to chart them."""
    base = f"http://{mock_metrics.host}:{mock_metrics.port}/solr"
    snap = await asyncio.to_thread(snapshot_node_solr, base)
    core = snap["cores"]["products.shard1.replica_n2"]
    assert core["segments"] == 7
    assert core["size_bytes"] == 1200577
    assert core["num_docs"] == 1240


async def test_missing_segment_metric_is_none_not_zero(aiohttp_server):
    """An older Solr without INDEX.segments must not be charted as 0 segments,
    which would look like a real, empty index."""
    async def metrics(request):
        return web.json_response({"metrics": {
            "solr.jvm": {},
            "solr.core.c.s.r": {"SEARCHER.searcher.numDocs": 5},
        }})

    app = web.Application()
    app.router.add_get("/solr/admin/metrics", metrics)
    server = await aiohttp_server(app)
    snap = await asyncio.to_thread(
        snapshot_node_solr, f"http://{server.host}:{server.port}/solr")
    assert snap["cores"]["c.s.r"]["segments"] is None


async def test_solr_cpu_is_normalized_to_percent(aiohttp_server):
    """Solr reports a 0..1 fraction where ES/OS report whole percent, and the
    dashboard draws one axis for both."""
    async def metrics(request):
        return web.json_response({"metrics": {"solr.jvm": {
            "os.processCpuLoad": 0.42,
            "os.systemCpuLoad": 0.771,
            "os.systemLoadAverage": 3.2,
        }}})

    app = web.Application()
    app.router.add_get("/solr/admin/metrics", metrics)
    server = await aiohttp_server(app)
    snap = await asyncio.to_thread(
        snapshot_node_solr, f"http://{server.host}:{server.port}/solr")
    assert snap["cpu"] == {"process_pct": 42.0, "host_pct": 77.1, "load1": 3.2}


async def test_solr_unreadable_cpu_is_none_not_negative(aiohttp_server):
    """A JVM that cannot read the figure returns -1, which would otherwise
    plot as a dip below the axis."""
    async def metrics(request):
        return web.json_response({"metrics": {"solr.jvm": {
            "os.processCpuLoad": -1.0, "os.systemCpuLoad": -1.0,
        }}})

    app = web.Application()
    app.router.add_get("/solr/admin/metrics", metrics)
    server = await aiohttp_server(app)
    snap = await asyncio.to_thread(
        snapshot_node_solr, f"http://{server.host}:{server.port}/solr")
    assert snap["cpu"]["process_pct"] is None
    assert snap["cpu"]["host_pct"] is None
