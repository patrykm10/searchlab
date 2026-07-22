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
