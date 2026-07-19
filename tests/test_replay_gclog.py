"""Tests for query-log replay and GC log analysis."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from solrlab.gclog import parse_gclog, summarize
from solrlab.replay import parse_log, replay

SOLR_LOG = """\
2026-07-09 12:00:00.100 INFO  (qtp1-21) [c:products s:shard1 r:core_node3 x:products_shard1_replica_n1] o.a.s.c.S.Request webapp=/solr path=/select params={q=body_t%3Alatency&rows=10} hits=42 status=0 QTime=3
2026-07-09 12:00:00.600 INFO  (qtp1-22) [c:products s:shard1] o.a.s.c.S.Request webapp=/solr path=/select params={q=*%3A*&fq=category_s%3Acat_1&fq=price_f%3A%5B10+TO+500%5D&rows=20} hits=9 status=0 QTime=7
2026-07-09 12:00:01.100 INFO  (qtp1-23) [c:products s:shard1] o.a.s.c.S.Request webapp=/solr path=/update params={commitWithin=5000} status=0 QTime=12
2026-07-09 12:00:01.850 WARN  (qtp1-24) [c:products] o.a.s.c.SolrCore slow query detected
2026-07-09 12:00:02.100 INFO  (qtp1-25) [c:products s:shard1] o.a.s.c.S.Request webapp=/solr path=/select params={q=title_t%3Amerge&start=0&rows=10} hits=3 status=0 QTime=2
"""

GC_LOG = """\
[2026-07-09T12:00:01.123+0000][10.500s][info][gc] GC(0) Pause Young (Normal) (G1 Evacuation Pause) 512M->128M(1024M) 12.345ms
[2026-07-09T12:00:11.123+0000][20.500s][info][gc] GC(1) Pause Young (Normal) (G1 Evacuation Pause) 540M->130M(1024M) 15.100ms
[2026-07-09T12:00:21.123+0000][30.500s][info][gc] GC(2) Pause Remark 300M->300M(1024M) 4.200ms
[2026-07-09T12:00:31.123+0000][40.500s][info][gc] GC(3) Pause Full (G1 Compaction Pause) 1000M->400M(1024M) 812.700ms
[2026-07-09T12:00:41.123+0000][50.500s][info][gc] GC(4) Pause Young (Normal) (G1 Evacuation Pause) 520M->125M(1024M) 11.900ms
[2026-07-09T12:00:41.200+0000][50.600s][info][gc,heap] GC(4) Eden regions: 100->0(100)
"""


# ------------------------------------------------------------------ parse ---

def test_parse_solr_log_filters_and_decodes(tmp_path):
    p = tmp_path / "solr.log"
    p.write_text(SOLR_LOG)
    entries = parse_log(p)  # default filter /select
    assert len(entries) == 3  # /update and WARN lines excluded
    assert entries[0]["offset_s"] == 0.0
    assert entries[1]["offset_s"] == pytest.approx(0.5)
    assert entries[2]["offset_s"] == pytest.approx(2.0)
    # URL decoding and repeated fq keys preserved
    q1 = dict(entries[0]["params"])
    assert q1["q"] == "body_t:latency"
    fqs = [v for k, v in entries[1]["params"] if k == "fq"]
    assert fqs == ["category_s:cat_1", "price_f:[10 TO 500]"]


def test_parse_solr_log_update_filter(tmp_path):
    p = tmp_path / "solr.log"
    p.write_text(SOLR_LOG)
    entries = parse_log(p, path_filter="/update")
    assert len(entries) == 1
    assert dict(entries[0]["params"])["commitWithin"] == "5000"


def test_parse_plain_file(tmp_path):
    p = tmp_path / "queries.txt"
    p.write_text("q=foo&rows=5\n\n# comment\nq=bar&fq=x:1\n")
    entries = parse_log(p)
    assert len(entries) == 2
    assert dict(entries[0]["params"])["q"] == "foo"
    assert dict(entries[1]["params"])["fq"] == "x:1"


def test_parse_empty_exits(tmp_path):
    p = tmp_path / "empty.log"
    p.write_text("# nothing here\n")
    with pytest.raises(SystemExit):
        parse_log(p)


# ----------------------------------------------------------------- replay ---

@pytest.fixture
async def mock_solr(aiohttp_server):
    seen: list[dict] = []

    async def select(request):
        seen.append(dict(request.query))
        await asyncio.sleep(0.003)
        return web.json_response({"response": {}})

    app = web.Application()
    app.router.add_get("/solr/test/select", select)
    server = await aiohttp_server(app)
    server.seen = seen
    return server


async def test_replay_original_pacing(mock_solr, tmp_path):
    p = tmp_path / "solr.log"
    p.write_text(SOLR_LOG)
    entries = parse_log(p)
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await replay(base, "test", entries, speed=2.0)  # 2s span -> ~1s
    assert len(result.records) == 3
    assert all(r.ok for r in result.records)
    assert 0.9 <= result.duration <= 1.6
    assert mock_solr.seen[0]["q"] == "body_t:latency"


async def test_replay_rps_override_and_loop(mock_solr, tmp_path):
    p = tmp_path / "queries.txt"
    p.write_text("q=a\nq=b\n")
    entries = parse_log(p)
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await replay(base, "test", entries, rps=20, loop_count=3)
    assert len(result.records) == 6
    assert result.duration == pytest.approx(6 / 20, abs=0.15)


# ------------------------------------------------------------------ gclog ---

def test_gclog_parse(tmp_path):
    p = tmp_path / "solr_gc.log"
    p.write_text(GC_LOG)
    pauses = parse_gclog(p)
    assert len(pauses) == 5
    kinds = [x.kind for x in pauses]
    assert kinds.count("Young") == 3 and "Full" in kinds and "Remark" in kinds
    full = next(x for x in pauses if x.kind == "Full")
    assert full.ms == pytest.approx(812.7)
    assert full.reclaimed_mb == pytest.approx(600)
    remark = next(x for x in pauses if x.kind == "Remark")
    assert remark.reclaimed_mb == pytest.approx(0)


def test_gclog_summary(tmp_path):
    p = tmp_path / "solr_gc.log"
    p.write_text(GC_LOG)
    out = summarize(parse_gclog(p), label="solr1")
    assert "5 pauses over 40s" in out
    assert "Full" in out and "!! 1 Full GC" in out
    assert "worst pause: 812.7 ms (Full)" in out
    assert "throughput lost to GC" in out


def test_gclog_empty():
    assert "no GC pauses" in summarize([], label="solr9")
