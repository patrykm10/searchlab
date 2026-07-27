"""Per-replica Lucene segment detail."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab.segments import replica_segments

# shaped like a real /admin/segments response, including the diagnostics
# block that carries where a segment came from
PAYLOAD = {
    "info": {"numSegments": 3, "commitLuceneVersion": "9.10.0"},
    "segments": {
        "_a": {"name": "_a", "size": 16250, "delCount": 250,
               "sizeInBytes": 46872722, "age": "2026-07-25T12:25:13.289Z",
               "version": "9.10.0", "diagnostics": {"source": "merge"}},
        "_b": {"name": "_b", "size": 900, "delCount": 0,
               "sizeInBytes": 1048576, "age": "2026-07-25T12:26:00.000Z",
               "version": "9.10.0", "diagnostics": {"source": "flush"}},
        "_c": {"name": "_c", "size": 100, "delCount": 0,
               "sizeInBytes": 524288, "age": "2026-07-25T12:26:10.000Z",
               "version": "9.10.0", "diagnostics": {"source": "flush"}},
    },
}


@pytest.fixture
async def mock_segments(aiohttp_server):
    async def segments(request):
        return web.json_response(PAYLOAD)

    app = web.Application()
    app.router.add_get("/solr/products_shard1_replica_n1/admin/segments", segments)
    return await aiohttp_server(app)


async def test_segments_sorted_largest_first(mock_segments):
    spec = ClusterSpec(base_port=mock_segments.port)
    out = await asyncio.to_thread(replica_segments, spec,
                                  "products_shard1_replica_n1")
    assert [s["name"] for s in out["segments"]] == ["_a", "_b", "_c"]
    assert out["segments"][0]["size"] == "44.7 MB"


async def test_summary_counts_docs_deletes_and_provenance(mock_segments):
    spec = ClusterSpec(base_port=mock_segments.port)
    s = (await asyncio.to_thread(replica_segments, spec,
                                 "products_shard1_replica_n1"))["summary"]
    assert s["count"] == 3
    assert s["docs"] == 17250            # live docs only
    assert s["deleted"] == 250
    # deleted share is of live+deleted, since deletes still occupy the index
    assert s["deleted_pct"] == pytest.approx(1.4, abs=0.1)
    # whether merging keeps up shows in the flush/merge split
    assert s["by_source"] == {"merge": 1, "flush": 2}
    assert s["lucene"] == "9.10.0"


async def test_per_segment_deleted_share_is_reported(mock_segments):
    spec = ClusterSpec(base_port=mock_segments.port)
    out = await asyncio.to_thread(replica_segments, spec,
                                  "products_shard1_replica_n1")
    biggest = out["segments"][0]
    assert biggest["deleted"] == 250
    assert biggest["deleted_pct"] == pytest.approx(1.5, abs=0.1)
    assert out["segments"][1]["deleted_pct"] == 0.0


async def test_missing_diagnostics_does_not_crash(aiohttp_server):
    """Older Solr, or a segment mid-write, may not carry diagnostics."""
    async def segments(request):
        return web.json_response({"info": {}, "segments": {
            "_x": {"name": "_x", "size": 5, "sizeInBytes": 100}}})

    app = web.Application()
    app.router.add_get("/solr/c/admin/segments", segments)
    server = await aiohttp_server(app)
    out = await asyncio.to_thread(
        replica_segments, ClusterSpec(base_port=server.port), "c")
    assert out["segments"][0]["source"] == "?"
    assert out["summary"]["count"] == 1
