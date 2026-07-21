"""Replica-management helpers and the log-event classifier."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec, add_replica, collection_detail, delete_replica
from searchlab.loglex import classify, is_noise


# ----------------------------------------------------------------- loglex ---

@pytest.mark.parametrize("line,tag", [
    ("o.a.s.u.DirectUpdateHandler2 start commit{flags=0,openSearcher=true}", "commit"),
    ("o.a.s.u.DirectUpdateHandler2 end_commit_flush", "commit done"),
    ("o.a.s.c.SolrCore [products] Registered new searcher autowarm time: 3 ms", "new searcher"),
    ("o.a.s.u.LoggingInfoStream [MS][qtp-45]: merge segment _4c into _4d", "merge"),
    ("o.a.s.c.ShardLeaderElectionContext I am the new leader", "leader election"),
    ("o.a.s.c.RecoveryStrategy Starting recovery process", "recovery"),
    ("o.a.s.s.HttpSolrCall webapp=/solr path=/update params={}", "indexing"),
    ("completely unremarkable chatter", None),
])
def test_classify(line, tag):
    event = classify(line)
    if tag is None:
        assert event is None
    else:
        assert event["tag"] == tag
        assert len(event["desc"]) > 20  # a real explanation, not a stub


@pytest.mark.parametrize("line,noisy", [
    ("2026-07-20 INFO (qtp-1) o.a.s.s.HttpSolrCall [admin] webapp=null path=/admin/metrics status=0 QTime=3", True),
    ("2026-07-20 INFO (qtp-2) o.a.s.c.S.Request [products] path=/select params={q=*:*} hits=42 QTime=1", True),
    ("2026-07-20 INFO (qtp-6) o.a.s.c.S.Request webapp=/solr path=/config params={wt=json} status=0 QTime=1", True),
    ("2026-07-20 INFO (qtp-7) o.a.s.c.S.Request webapp=/solr path=/config/overlay params={wt=json} status=0 QTime=0", True),
    ("2026-07-20 INFO (qtp-3) o.a.s.u.p.LogUpdateProcessorFactory [products] path=/update params={} QTime=14", False),
    ("2026-07-20 WARN (qtp-4) o.a.s.s.HttpSolrCall [admin] path=/admin/metrics timed out", False),
    ("2026-07-20 INFO (qtp-5) o.a.s.u.DirectUpdateHandler2 start commit{openSearcher=true}", False),
])
def test_is_noise_drops_routine_chatter_only(line, noisy):
    assert is_noise(line) is noisy


def test_logstream_skips_noise():
    from pathlib import Path

    from searchlab.logstream import LogStream
    ls = LogStream(compose_file=Path("/dev/null"), maxlen=10)
    ls._append("x | 2026-07-20 10:00:00.000 INFO (q) [c:] o.a.s.s.HttpSolrCall path=/admin/metrics QTime=1")
    ls._append("x | 2026-07-20 10:00:01.000 INFO (q) [c:] o.a.s.u.DirectUpdateHandler2 start commit{}")
    latest, lines = ls.since(0)
    assert latest == 1
    assert len(lines) == 1
    assert "commit" in lines[0][1]


def test_classify_specific_beats_generic():
    # openSearcher=true must hit the visibility description, not the durability one
    event = classify("start commit{flags=0,optimize=false,openSearcher=true}")
    assert "searchable" in event["desc"]
    event = classify("start commit{flags=0,optimize=false,openSearcher=false}")
    assert "Durability" in event["desc"]


# ------------------------------------------------------------- replica API ---

CLUSTERSTATUS = {
    "cluster": {"collections": {"products": {"shards": {
        "shard1": {"state": "active", "replicas": {
            "core_node3": {"core": "products_shard1_replica_n1",
                           "node_name": "solr1:8983_solr", "type": "NRT",
                           "state": "active", "leader": "true"},
            "core_node5": {"core": "products_shard1_replica_t2",
                           "node_name": "solr2:8983_solr", "type": "TLOG",
                           "state": "recovering"},
        }},
    }}}}}


@pytest.fixture
async def mock_collections_api(aiohttp_server):
    seen = {}

    async def admin(request):
        seen.update(request.rel_url.query)
        if request.rel_url.query.get("action") == "CLUSTERSTATUS":
            return web.json_response(CLUSTERSTATUS)
        return web.json_response({"responseHeader": {"status": 0}})

    app = web.Application()
    app.router.add_get("/solr/admin/collections", admin)
    server = await aiohttp_server(app)
    server.seen = seen
    return server


async def test_collection_detail_parses_topology(mock_collections_api):
    spec = ClusterSpec(base_port=mock_collections_api.port)
    detail = await asyncio.to_thread(collection_detail, spec, "products")
    reps = detail["shards"]["shard1"]["replicas"]
    assert reps["core_node3"]["leader"] is True
    assert reps["core_node3"]["node"] == "solr1"
    assert reps["core_node5"]["type"] == "TLOG"
    assert reps["core_node5"]["state"] == "recovering"
    assert reps["core_node5"]["leader"] is False


async def test_add_replica_params_and_type_validation(mock_collections_api):
    spec = ClusterSpec(base_port=mock_collections_api.port)
    await asyncio.to_thread(add_replica, spec, "products", "shard1", "PULL")
    assert mock_collections_api.seen["action"] == "ADDREPLICA"
    assert mock_collections_api.seen["type"] == "PULL"
    assert mock_collections_api.seen["shard"] == "shard1"

    with pytest.raises(ValueError, match="Replica type"):
        await asyncio.to_thread(add_replica, spec, "products", "shard1", "SPICY")


async def test_delete_replica_params(mock_collections_api):
    spec = ClusterSpec(base_port=mock_collections_api.port)
    await asyncio.to_thread(delete_replica, spec, "products", "shard1", "core_node5")
    assert mock_collections_api.seen["action"] == "DELETEREPLICA"
    assert mock_collections_api.seen["replica"] == "core_node5"
