"""Write-path walkthrough: what a write actually did to a replica's segments."""

from __future__ import annotations

from searchlab.actions import ActionRunner
from searchlab.cluster import ClusterSpec
from searchlab.segments import diff_segments


def seg(name, source, docs=10):
    return {"name": name, "source": source, "docs": docs, "size": "1 KB"}


def test_new_flush_segment_is_reported_as_flush():
    d = diff_segments([seg("_a", "merge")], [seg("_a", "merge"), seg("_b", "flush")])
    assert [s["name"] for s in d["new"]] == ["_b"]
    assert d["gone"] == []
    assert d["by_source"] == {"flush": 1}


def test_merge_shows_inputs_gone_and_result_new():
    before = [seg("_a", "flush"), seg("_b", "flush"), seg("_c", "flush")]
    after = [seg("_d", "merge", docs=30)]
    d = diff_segments(before, after)
    assert [s["name"] for s in d["new"]] == ["_d"]
    assert sorted(s["name"] for s in d["gone"]) == ["_a", "_b", "_c"]
    assert d["by_source"] == {"merge": 1}


def test_first_write_to_empty_replica_is_all_new():
    d = diff_segments([], [seg("_0", "flush")])
    assert len(d["new"]) == 1 and d["gone"] == []


def test_nothing_changed():
    same = [seg("_a", "flush")]
    assert diff_segments(same, same) == {"new": [], "gone": [], "by_source": {}}


# ---- ActionRunner: the baseline the dashboard never has to track itself ----

def runner_with(states, engine="solr"):
    """A runner whose segments() replays `states` one call at a time."""
    r = ActionRunner(ClusterSpec(engine=engine))
    calls = iter(states)
    r.segments = lambda collection, core: {
        "ok": True, "core": core, "segments": next(calls),
        "summary": {"count": 0, "docs": 0}}
    return r


def test_diff_needs_a_snapshot_first():
    r = runner_with([[seg("_a", "flush")]])
    out = r.writepath_diff("products", "core1")
    assert not out["ok"] and "snapshot" in out["error"].lower()


def test_baseline_rolls_forward_so_a_merge_reads_as_a_second_step():
    flushed = [seg("_a", "flush"), seg("_b", "flush")]
    r = runner_with([
        [seg("_a", "flush")],           # snapshot
        flushed,                        # diff 1: a flush
        [seg("_c", "merge", docs=20)],  # diff 2: the merge
    ])
    assert r.writepath_snapshot("products", "core1")["ok"]

    d1 = r.writepath_diff("products", "core1")["diff"]
    assert [s["name"] for s in d1["new"]] == ["_b"] and d1["gone"] == []

    # measured against the flush state, not the original snapshot
    d2 = r.writepath_diff("products", "core1")["diff"]
    assert [s["name"] for s in d2["new"]] == ["_c"]
    assert sorted(s["name"] for s in d2["gone"]) == ["_a", "_b"]


def test_baselines_are_per_replica():
    r = runner_with([[seg("_a", "flush")], [seg("_x", "flush")]])
    r.writepath_snapshot("products", "core1")
    assert not r.writepath_diff("products", "core2")["ok"]


def test_each_owner_rolls_its_own_baseline():
    # The segment panel and the walkthrough diff the same replica. With one
    # shared baseline, whichever diffed first used up the other's change.
    a, b = [seg("_a", "flush")], [seg("_a", "flush"), seg("_b", "flush")]
    r = runner_with([a, a, b, b])
    r.writepath_snapshot("products", "core1", owner="panel:t1")
    r.writepath_snapshot("products", "core1", owner="wp:t1")
    assert [s["name"] for s in r.writepath_diff("products", "core1", owner="wp:t1")
            ["diff"]["new"]] == ["_b"]
    # the walkthrough's diff didn't move the panel's baseline
    assert [s["name"] for s in r.writepath_diff("products", "core1", owner="panel:t1")
            ["diff"]["new"]] == ["_b"]
    # and an owner that never took a snapshot still has to
    assert not r.writepath_diff("products", "core1", owner="panel:t2")["ok"]


def test_refused_on_es_and_os():
    for engine in ("opensearch", "elasticsearch"):
        r = runner_with([[]], engine=engine)
        out = r.writepath_snapshot("products", "0")
        assert not out["ok"] and "Solr-only" in out["error"]


# ---- one document through a fake Solr --------------------------------------

import asyncio

import pytest
from aiohttp import web

from searchlab.writepath import (doc_visibility, fetch_analysis,
                                 index_single_doc, parse_stages)

# default json.nl=flat: each stage is [className, tokens] flattened in order
ANALYSIS = {"analysis": {"field_types": {}, "field_names": {"title_t": {"index": [
    "org.apache.lucene.analysis.standard.StandardTokenizer",
    [{"text": "Running", "position": 1, "type": "<ALPHANUM>"},
     {"text": "Shoes", "position": 2, "type": "<ALPHANUM>"}],
    "org.apache.lucene.analysis.core.LowerCaseFilter",
    [{"text": "running", "position": 1, "type": "<ALPHANUM>"},
     {"text": "shoes", "position": 2, "type": "<ALPHANUM>"}],
]}}}}


def test_stages_keep_order_and_short_names():
    stages = parse_stages(ANALYSIS["analysis"]["field_names"]["title_t"]["index"])
    assert [s["stage"] for s in stages] == ["StandardTokenizer", "LowerCaseFilter"]
    assert [t["text"] for t in stages[1]["tokens"]] == ["running", "shoes"]


def test_char_filter_output_is_one_string():
    stages = parse_stages(["o.a.l.a.charfilter.HTMLStripCharFilter", "Running Shoes",
                           "o.a.l.a.standard.StandardTokenizer", [{"text": "Running"}]])
    assert stages[0]["tokens"] == [{"text": "Running Shoes", "position": None, "type": "text"}]


def test_other_namedlist_encodings_parse_the_same():
    flat = ANALYSIS["analysis"]["field_names"]["title_t"]["index"]
    arrarr = [[flat[0], flat[1]], [flat[2], flat[3]]]
    assert parse_stages(arrarr) == parse_stages(flat)


@pytest.fixture
async def fake_solr(aiohttp_server):
    seen = {}

    async def analysis(request):
        seen["analysis"] = dict(request.query)
        return web.json_response(ANALYSIS)

    async def update(request):
        seen["update_query"] = dict(request.query)
        seen["update_body"] = await request.json()
        return web.json_response({"responseHeader": {"status": 0}})

    async def rtg(request):
        return web.json_response({"doc": {"id": request.query["id"]}})

    async def select(request):
        seen["select"] = dict(request.query)
        return web.json_response({"response": {"numFound": 0}})

    app = web.Application()
    app.router.add_get("/solr/products/analysis/field", analysis)
    app.router.add_post("/solr/products/update", update)
    app.router.add_get("/solr/products/get", rtg)
    app.router.add_get("/solr/products/select", select)
    server = await aiohttp_server(app)
    server.seen = seen
    return server


async def test_fetch_analysis_asks_for_the_field_and_value(fake_solr):
    spec = ClusterSpec(base_port=fake_solr.port)
    stages = await asyncio.to_thread(fetch_analysis, spec, "products", "title_t", "Running Shoes")
    assert fake_solr.seen["analysis"]["analysis.fieldname"] == "title_t"
    assert fake_solr.seen["analysis"]["analysis.fieldvalue"] == "Running Shoes"
    assert len(stages) == 2


async def test_single_doc_is_added_without_any_commit(fake_solr):
    spec = ClusterSpec(base_port=fake_solr.port)
    out = await asyncio.to_thread(index_single_doc, spec, "products", "title_t", "hello")
    q = fake_solr.seen["update_query"]
    # the walkthrough's point is that nothing commits it behind your back
    assert "commitWithin" not in q and "commit" not in q and "softCommit" not in q
    assert fake_solr.seen["update_body"] == [{"id": out["id"], "title_t": "hello"}]
    assert out["took_ms"] >= 0


async def test_visibility_reports_rtg_and_search_separately(fake_solr):
    spec = ClusterSpec(base_port=fake_solr.port)
    out = await asyncio.to_thread(doc_visibility, spec, "products", "wp-abc-1")
    assert out == {"rtg": True, "searchable": False, "doc": {"id": "wp-abc-1"}}
    # the id's hyphens are escaped, or the query parser reads them as operators
    assert fake_solr.seen["select"]["q"] == r"id:wp\-abc\-1"


def test_submit_refuses_missing_inputs():
    r = ActionRunner(ClusterSpec())
    assert "field" in r.writepath_submit("products", "", "x")["error"]
    assert "collection" in r.writepath_submit("", "f", "x")["error"]


def test_submit_refuses_the_id_and_internal_fields():
    # field="id" replaced the generated id: the document went in under the
    # user's value (maybe over a real one) and the walkthrough waited forever
    r = ActionRunner(ClusterSpec())
    for field in ("id", "_version_", "_root_"):
        out = r.writepath_submit("products", field, "x")
        assert not out["ok"] and "text field" in out["error"], field


# ---- the dashboard section ---------------------------------------------------

from pathlib import Path
import re

HTML = (Path(__file__).parent.parent / "searchlab" / "templates" / "dashboard.html").read_text()


def test_walkthrough_diagram_has_the_four_stages_in_order():
    stages = re.findall(r'class="wp-box" data-stage="(\w+)"', HTML)
    assert stages == ["analyze", "buffer", "segment", "merge"]


def test_every_walkthrough_button_is_wired():
    for bid in re.findall(r'id="(btn-wp-[\w-]+)"', HTML):
        assert f'$("{bid}").addEventListener' in HTML, bid


def test_walkthrough_calls_only_routes_the_server_has():
    server = (Path(__file__).parent.parent / "searchlab" / "dashboard.py").read_text()
    literal = re.findall(r'"/api/writepath/(\w+)"', HTML)
    # segDiffAction builds its route from the segment panel button's data-wp
    built = re.findall(r'data-wp="(\w+)"', HTML)
    assert literal and built
    for kind in set(literal) | set(built):
        assert f'"/api/writepath/{kind}"' in server, kind


def test_page_keeps_its_runs_and_baselines_apart():
    page = (Path(__file__).parent.parent / "searchlab" / "templates" / "dashboard.html").read_text()
    # each consumer names its own baseline
    assert 'owner: "panel:" + TAB' in page and 'owner: "wp:" + TAB' in page
    # the watcher looks only at the shard holding the document
    assert "run.docCore ? [run.docCore] : run.cores" in page
    # every timer-driven step drops out once its run is gone (Start over, a new run)
    body = page[page.index("function wpWatchFlush"):page.index("async function wpCommit")]
    assert body.count("if (wp !== run) return;") >= 2
    assert "A new searcher opened" in body and "On disk, still not searchable" in body
