"""Engine abstraction tests: compose rendering, wire formats, mock ES loop."""

from __future__ import annotations

import asyncio
import json
import random

import pytest
import yaml
from aiohttp import web

from searchlab.chaos import _container
from searchlab.cluster import ClusterSpec, render_compose
from searchlab.engines import get_engine
from searchlab.indexer import index_file
from searchlab.loadtest import QueryPicker, run_load


def test_engine_aliases_and_unknown():
    assert get_engine("es").name == "elasticsearch"
    assert get_engine("os").name == "opensearch"
    with pytest.raises(SystemExit):
        get_engine("sphinx")


@pytest.mark.parametrize("engine,prefix,image_bit", [
    ("elasticsearch", "es", "docker.elastic.co/elasticsearch"),
    ("opensearch", "os", "opensearchproject/opensearch"),
])
def test_es_family_compose_renders(engine, prefix, image_bit):
    spec = ClusterSpec(engine=engine, solr_version="8.14.3", solr_nodes=3,
                       heap="512m", base_port=9200)
    parsed = yaml.safe_load(render_compose(spec))
    services = parsed["services"]
    assert set(services) == {f"{prefix}1", f"{prefix}2", f"{prefix}3"}
    n1 = services[f"{prefix}1"]
    assert image_bit in n1["image"]
    assert "9200:9200" in n1["ports"]
    env = n1["environment"]
    assert "discovery.seed_hosts" in env and f"{prefix}2" in env["discovery.seed_hosts"]
    java_opts = env.get("ES_JAVA_OPTS") or env.get("OPENSEARCH_JAVA_OPTS")
    assert "-Xms512m -Xmx512m" in java_opts
    if engine == "elasticsearch":
        assert env["xpack.security.enabled"] == "false"
    else:
        assert env["DISABLE_SECURITY_PLUGIN"] == "true"


def test_es_single_node_discovery():
    spec = ClusterSpec(engine="elasticsearch", solr_nodes=1, base_port=9200)
    env = yaml.safe_load(render_compose(spec))["services"]["es1"]["environment"]
    assert env["discovery.type"] == "single-node"
    assert "cluster.initial_master_nodes" not in env


def test_solr_compose_unchanged():
    parsed = yaml.safe_load(render_compose(ClusterSpec()))
    assert "solr1" in parsed["services"] and "zk1" in parsed["services"]


def test_bulk_request_formats():
    docs = [{"id": "a", "x": 1}, {"x": 2}]
    solr = get_engine("solr").bulk_request("http://h/solr", "c", docs, 5000)
    assert solr["url"].endswith("/c/update") and solr["json"] == docs
    es = get_engine("es").bulk_request("http://h", "c", docs, 5000)
    assert es["url"].endswith("/c/_bulk")
    lines = es["content"].strip().split("\n")
    assert len(lines) == 4
    assert json.loads(lines[0]) == {"index": {"_id": "a"}}
    assert json.loads(lines[2]) == {"index": {}}
    assert es["headers"]["Content-Type"] == "application/x-ndjson"


def test_recursive_body_substitution():
    picker = QueryPicker(
        [{"name": "t", "weight": 1,
          "body": {"query": {"match": {"f": "{RAND_WORD}"}},
                   "filters": [{"range": {"p": {"gte": "{RAND_INT:1:3}"}}}]}}],
        random.Random(0), ["alpha"],
    )
    t = picker.pick_template()
    assert t["body"]["query"]["match"]["f"] == "alpha"
    assert 1 <= int(t["body"]["filters"][0]["range"]["p"]["gte"]) <= 3


def test_chaos_node_names_per_engine():
    solr_spec = ClusterSpec(solr_nodes=2, zk_nodes=1)
    assert _container(solr_spec, "2") == "searchlab-solr2"
    assert _container(solr_spec, "zk1") == "searchlab-zk1"
    es_spec = ClusterSpec(engine="elasticsearch", solr_nodes=2)
    assert _container(es_spec, "2") == "searchlab-es2"
    with pytest.raises(SystemExit):
        _container(es_spec, "zk1")  # no ZooKeeper in the ES world


# ------------------------------------------------------- mock ES end-to-end ---

NODES_STATS = {
    "nodes": {"abc": {
        "jvm": {"mem": {"heap_used_in_bytes": 300 * 2**20, "heap_max_in_bytes": 1024 * 2**20},
                "gc": {"collectors": {"young": {"collection_count": 42, "collection_time_in_millis": 810},
                                      "old": {"collection_count": 1, "collection_time_in_millis": 95}}}},
        "indices": {"docs": {"count": 12345, "deleted": 67},
                    "query_cache": {"hit_count": 80, "miss_count": 20, "evictions": 3, "cache_count": 10},
                    "request_cache": {"hit_count": 0, "miss_count": 0, "evictions": 0},
                    "indexing": {"index_total": 9999}, "flush": {"total": 4},
                    "refresh": {"total": 120}, "merges": {"total": 7}},
    }}
}


@pytest.fixture
async def mock_es(aiohttp_server):
    state = {"bulks": 0, "docs": 0, "searches": []}

    async def bulk(request):
        payload = await request.text()
        state["bulks"] += 1
        state["docs"] += sum(1 for ln in payload.strip().split("\n")) // 2
        return web.json_response({"errors": False, "items": []})

    async def search(request):
        state["searches"].append(await request.json())
        await asyncio.sleep(0.003)
        return web.json_response({"hits": {"total": {"value": 1}, "hits": []}})

    async def refresh(request):
        return web.json_response({"_shards": {"failed": 0}})

    async def stats(request):
        return web.json_response(NODES_STATS)

    app = web.Application()
    app.router.add_post("/idx/_bulk", bulk)
    app.router.add_post("/idx/_search", search)
    app.router.add_post("/idx/_refresh", refresh)
    app.router.add_get("/_nodes/_local/stats/jvm,indices,thread_pool,breaker", stats)
    server = await aiohttp_server(app)
    server.state = state
    return server


async def test_index_file_es(mock_es, tmp_path):
    data = tmp_path / "d.jsonl"
    data.write_text("\n".join(json.dumps({"id": f"d{i}", "v": i}) for i in range(25)))
    base = f"http://{mock_es.host}:{mock_es.port}"
    stats = await index_file(base, "idx", data, threads=2, batch_size=10, engine="elasticsearch")
    assert stats.docs == 25 and stats.errors == 0
    assert mock_es.state["bulks"] == 3 and mock_es.state["docs"] == 25


async def test_run_load_es(mock_es):
    base = f"http://{mock_es.host}:{mock_es.port}"
    result = await run_load(base, "idx", rps=40, duration=2, seed=1, engine="elasticsearch")
    assert len(result.records) > 60 and all(r.ok for r in result.records)
    assert mock_es.state["searches"][0] == {"query": {"match_all": {}}, "size": 10}


async def test_es_metrics_normalization(mock_es):
    eng = get_engine("elasticsearch")
    # snapshot_node uses a sync client; run it off-loop so the mock can respond
    snap = await asyncio.to_thread(
        eng.snapshot_node, f"http://{mock_es.host}:{mock_es.port}")
    assert snap["jvm"]["heap_used_mb"] == 300.0
    assert snap["jvm"]["gc"]["young"] == {"count": 42, "time": 810}
    core = snap["cores"]["indices (node total)"]
    assert core["num_docs"] == 12345
    assert core["caches"]["queryCache"]["hitratio"] == 0.8
    assert core["caches"]["requestCache"]["hitratio"] is None  # 0/0 traffic
    assert core["update"]["adds_cumulative"] == 9999
    assert core["update"]["merges_minor"] == 7


def test_shipped_es_queries_load():
    from pathlib import Path

    from searchlab.loadtest import load_queries
    templates = load_queries(Path(__file__).parent.parent / "queries" / "es-default.yaml")
    assert any("body" in t for t in templates)


# ------------------------------------------------- ES schema + slow logs ---

def test_es_mappings_from_profile():
    from searchlab.schema import mappings_from_profile
    profile = {"fields": {
        "id": {"type": "id"},
        "body_t": {"type": "text"},
        "cat_s": {"type": "categorical", "cardinality": 5},
        "price_f": {"type": "float"},
        "ts_dt": {"type": "date"},
        "tags_ss": {"type": "multivalued", "of": {"type": "categorical", "cardinality": 3}},
        "uid_s": {"type": "keyword", "es": {"doc_values": False}},
    }}
    props = mappings_from_profile(profile)["properties"]
    assert "id" not in props
    assert props["body_t"] == {"type": "text"}
    assert props["cat_s"] == {"type": "keyword"}
    assert props["price_f"] == {"type": "float"}
    assert props["ts_dt"]["type"] == "date"
    assert props["tags_ss"] == {"type": "keyword"}  # arrays are implicit in ES
    assert props["uid_s"] == {"type": "keyword", "doc_values": False}


CLASSIC_SLOWLOG = """\
[2026-07-09T12:00:00,100][WARN ][i.s.s.query] [es1] [products][0] took[12ms], took_millis[12], total_hits[42 hits], source[{"query":{"match":{"body_t":"latency"}},"size":10}]
[2026-07-09T12:00:00,900][WARN ][i.s.s.query] [es1] [products][1] took[7ms], took_millis[7], total_hits[9 hits], source[{"query":{"match_all":{}},"size":0,"aggs":{"c":{"terms":{"field":"cat"}}}}]
some unrelated log line
"""

JSON_SLOWLOG = """\
{"@timestamp":"2026-07-09T12:00:00.100Z","log.level":"WARN","elasticsearch.slowlog.took_millis":"12","elasticsearch.slowlog.source":"{\\"query\\":{\\"match\\":{\\"f\\":\\"x\\"}}}"}
{"@timestamp":"2026-07-09T12:00:02.100Z","log.level":"WARN","elasticsearch.slowlog.took_millis":"3","elasticsearch.slowlog.source":"{\\"query\\":{\\"match_all\\":{}}}"}
"""


def test_parse_classic_slowlog(tmp_path):
    from searchlab.replay import parse_log
    p = tmp_path / "slow.log"
    p.write_text(CLASSIC_SLOWLOG)
    entries = parse_log(p, engine="elasticsearch")
    assert len(entries) == 2
    assert entries[0]["offset_s"] == 0.0
    assert entries[1]["offset_s"] == pytest.approx(0.8)
    assert entries[0]["body"]["query"]["match"]["body_t"] == "latency"
    assert "aggs" in entries[1]["body"]


def test_parse_json_slowlog(tmp_path):
    from searchlab.replay import parse_log
    p = tmp_path / "slow.json.log"
    p.write_text(JSON_SLOWLOG)
    entries = parse_log(p, engine="opensearch")
    assert len(entries) == 2
    assert entries[1]["offset_s"] == pytest.approx(2.0)
    assert entries[0]["body"]["query"]["match"]["f"] == "x"


async def test_replay_slowlog_against_mock_es(mock_es, tmp_path):
    from searchlab.replay import parse_log, replay
    p = tmp_path / "slow.log"
    p.write_text(CLASSIC_SLOWLOG)
    entries = parse_log(p, engine="elasticsearch")
    base = f"http://{mock_es.host}:{mock_es.port}"
    result = await replay(base, "idx", entries, speed=4.0)
    assert len(result.records) == 2 and all(r.ok for r in result.records)
    assert result.records[0].template == "/_search"
    assert mock_es.state["searches"][0]["query"]["match"]["body_t"] == "latency"
