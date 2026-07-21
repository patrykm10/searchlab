"""Replica-management helpers and the log-event classifier."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec, add_replica, collection_detail, delete_replica
from searchlab.loglex import classify, is_noise, parse_request


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


# ------------------------------------------------------------ traffic ---
# Real lines captured from a running searchlab cluster.

QUERY_LINE = (
    "searchlab-solr1  | 2026-07-21 06:46:28.041 INFO  (qtp-82-solr1-49699) "
    "[c:products s:shard2 r:core_node8 x:products_shard2_replica_n7 t:solr1-49699] "
    "o.a.s.c.S.Request webapp=/solr path=/select "
    "params={q=title_t:boost&fq=price_f:[49+TO+1731]&rows=10&wt=json} "
    "rid=solr1-49699 hits=257 status=0 QTime=2")

SHARD_LINE = (
    "searchlab-solr1  | 2026-07-21 06:46:28.040 INFO  (qtp-72) "
    "[c:products s:shard2 x:products_shard2_replica_n7] "
    "o.a.s.c.S.Request webapp=/solr path=/select "
    "params={distrib=false&q=title_t:boost&isShard=true&wt=javabin} status=0 QTime=0")

UPDATE_LINE = (
    "searchlab-solr2  | 2026-07-21 06:41:06.412 INFO  (qtp-24) "
    "[c:products s:shard1 r:core_node4 x:products_shard1_replica_n2] "
    "o.a.s.u.p.LogUpdateProcessorFactory [products_shard1_replica_n2]  "
    "webapp=/solr path=/update params={commitWithin=10000&overwrite=true&wt=json}"
    "{add=[doc-0 (1784), doc-1 (1784), doc-2 (1784)]} 0 25")


def test_parse_query_request():
    e = parse_request(QUERY_LINE)
    assert e["kind"] == "query"
    assert e["internal"] is False
    assert e["hits"] == 257 and e["qtime"] == 2 and e["status"] == 0
    assert e["node"] == "solr1"
    assert e["core"] == "products_shard2_replica_n7"   # no trailing bracket
    assert "q=title_t:boost" in e["detail"]
    assert "fq=price_f:[49 TO 1731]" in e["detail"]    # + decoded to space
    assert "wt=" not in e["detail"] and "rid=" not in e["detail"]  # plumbing hidden


def test_parse_flags_internal_shard_fanout():
    e = parse_request(SHARD_LINE)
    assert e["kind"] == "query"
    assert e["internal"] is True   # caller drops these


def test_parse_update_request():
    e = parse_request(UPDATE_LINE)
    assert e["kind"] == "index"
    assert e["docs"] == 3
    assert "3 docs added" in e["detail"]
    assert e["status"] == 0 and e["qtime"] == 25


def test_parse_update_uses_solr_stated_total_not_listed_ids():
    """Solr logs only the first ~10 ids then the real total; counting the
    listed ids would report 11 docs for a 500-doc batch."""
    line = (
        "searchlab-solr1  | 2026-07-21 11:46:51.000 INFO  (qtp-19) "
        "[c:products s:shard1 x:products_shard1_replica_n2] "
        "o.a.s.u.p.LogUpdateProcessorFactory webapp=/solr path=/update "
        "params={commitWithin=10000&wt=json}"
        "{add=[doc-0 (187), doc-1 (187), doc-2 (187), ... (500 adds)]} 0 240")
    e = parse_request(line)
    assert e["docs"] == 500
    assert "500 docs added" in e["detail"]


def test_parse_flags_leader_replication_internal():
    """A batch the client sent once is logged again as the leader replicates
    it onward; counting both would double every indexing request."""
    line = (
        "searchlab-solr1  | 2026-07-21 11:45:13.851 INFO  (qtp-19) "
        "[c:products s:shard2 x:products_shard2_replica_n7] "
        "o.a.s.u.p.LogUpdateProcessorFactory webapp=/solr path=/update "
        "params={update.distrib=FROMLEADER&distrib.from=http://solr2:8983/solr/x/&wt=javabin}"
        "{add=[doc-4005 (187), doc-4006 (187)]} 0 3")
    e = parse_request(line)
    assert e["kind"] == "index"
    assert e["internal"] is True


def test_parse_ignores_non_request_lines():
    assert parse_request(
        "solr1 | 2026-07-21 06:41:06.000 INFO (x) "
        "o.a.s.u.DirectUpdateHandler2 start commit{openSearcher=true}") is None


def test_logstream_routes_traffic_away_from_activity():
    from pathlib import Path

    from searchlab.logstream import LogStream
    ls = LogStream(compose_file=Path("/dev/null"))
    ls._append(QUERY_LINE)
    ls._append(SHARD_LINE)     # internal: dropped entirely
    ls._append(UPDATE_LINE)
    ls._append("searchlab-solr1  | 2026-07-21 06:41:06.000 INFO  (x) [c:] "
               "o.a.s.u.DirectUpdateHandler2 start commit{openSearcher=true}")

    _, activity = ls.since(0)
    _, traffic = ls.traffic_since(0)
    assert len(traffic) == 2                       # query + update, no fan-out
    assert [r[1]["kind"] for r in traffic] == ["query", "index"]
    assert len(activity) == 1                      # only the commit event
    assert activity[0][2]["tag"] == "commit"


def test_logstream_drops_multiline_json_fragments():
    """Solr pretty-prints multi-line JSON for Overseer state changes; the
    fragments have no timestamp and would show as contextless events."""
    from pathlib import Path

    from searchlab.logstream import LogStream
    ls = LogStream(compose_file=Path("/dev/null"))
    ls._append("searchlab-solr1  | 2026-07-21 06:41:05.488 INFO  (Overseer) [c:] "
               "o.a.s.c.o.SliceMutator createReplica() {")
    ls._append('searchlab-solr1  |   "core":"products_shard1_replica_n2",')
    ls._append('searchlab-solr1  |   "state":"down",')
    ls._append('searchlab-solr1  |   "operation":"ADDREPLICA"}')
    _, activity = ls.since(0)
    assert len(activity) == 1   # the header record only, fragments dropped


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
