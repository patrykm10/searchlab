"""searchlab test suite. Run with: pytest -q

Covers everything that doesn't need a Docker daemon: data generation shape and
determinism, compose rendering, query template substitution, open-loop
scheduler accuracy (against an in-process mock Solr), drop accounting under
saturation, and report generation/comparison.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from searchlab.cluster import ClusterSpec, render_compose
from searchlab.datagen import generate, load_profile
from searchlab.loadtest import QueryPicker, load_queries, run_load, save_report
from searchlab.report import compare_text, html_compare, html_report, load_report

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- datagen ---

def test_datagen_deterministic_with_seed():
    profile = load_profile(ROOT / "profiles" / "default.yaml")
    a = list(generate(profile, 200, seed=42))
    b = list(generate(profile, 200, seed=42))
    assert a == b


def test_datagen_uuid_ids_deterministic_with_seed():
    profile = load_profile(ROOT / "profiles" / "high-cardinality.yaml")
    a = list(generate(profile, 100, seed=1))
    b = list(generate(profile, 100, seed=1))
    assert a == b
    assert len({d["id"] for d in a}) == 100  # unique ids


def test_datagen_cardinality_respected():
    profile = {"fields": {"cat_s": {"type": "categorical", "cardinality": 7}}}
    docs = list(generate(profile, 2000, seed=3))
    values = {d["cat_s"] for d in docs}
    assert len(values) == 7


def test_datagen_zipf_skews_distribution():
    profile = {"fields": {"cat_s": {"type": "categorical", "cardinality": 20, "zipf": 1.5}}}
    counts = Counter(d["cat_s"] for d in generate(profile, 5000, seed=3))
    top = counts.most_common(1)[0]
    # With zipf 1.5 over 20 values, the head value should dominate uniform share (250).
    assert top[0] == "cat_s_0"
    assert top[1] > 800


def test_datagen_text_word_bounds():
    profile = {"fields": {"t": {"type": "text", "min_words": 3, "max_words": 6}}}
    for d in generate(profile, 300, seed=9):
        assert 3 <= len(d["t"].split()) <= 6


def test_datagen_multivalued():
    profile = {"fields": {"tags_ss": {"type": "multivalued", "min_values": 2, "max_values": 4,
                                      "of": {"type": "categorical", "cardinality": 5}}}}
    for d in generate(profile, 100, seed=4):
        assert isinstance(d["tags_ss"], list)
        assert 2 <= len(d["tags_ss"]) <= 4


# ---------------------------------------------------------------- compose ---

@pytest.mark.parametrize("spec", [
    ClusterSpec(),
    ClusterSpec(solr_nodes=3, zk_nodes=3, monitoring=True, gc_logs=True,
                gc_tune="-XX:+UseG1GC", solr_opts="-Dfoo=bar"),
    ClusterSpec(solr_nodes=1, heap="512m", base_port=9000),
])
def test_compose_renders_valid_yaml(spec):
    parsed = yaml.safe_load(render_compose(spec))
    services = parsed["services"]
    assert len([s for s in services if s.startswith("solr") and s[4:].isdigit()]) == spec.solr_nodes
    assert len([s for s in services if s.startswith("zk")]) == spec.zk_nodes
    if spec.monitoring:
        assert {"prometheus", "grafana", "solr-exporter"} <= set(services)
    solr1 = services["solr1"]
    assert solr1["environment"]["SOLR_HEAP"] == spec.heap
    assert f"{spec.base_port}:8983" in solr1["ports"]


def test_compose_gc_tune_propagates():
    spec = ClusterSpec(gc_tune="-XX:+UseZGC")
    parsed = yaml.safe_load(render_compose(spec))
    assert parsed["services"]["solr1"]["environment"]["GC_TUNE"] == "-XX:+UseZGC"


# ---------------------------------------------------------------- queries ---

def test_query_picker_substitution():
    picker = QueryPicker(
        [{"name": "t", "weight": 1,
          "params": {"q": "body_t:{RAND_WORD}", "fq": "p:[{RAND_INT:1:5} TO {RAND_INT:10:20}]"}}],
        random.Random(0), ["alpha", "beta"],
    )
    name, params = picker.pick()
    assert name == "t"
    assert params["q"].split(":")[1] in ("alpha", "beta")
    lo, hi = params["fq"][3:-1].split(" TO ")
    assert 1 <= int(lo) <= 5 and 10 <= int(hi) <= 20
    assert params["wt"] == "json"


def test_query_weight_zero_never_picked():
    picker = QueryPicker(
        [{"name": "on", "weight": 1, "params": {"q": "*:*"}},
         {"name": "off", "weight": 0, "params": {"q": "*:*"}}],
        random.Random(0), ["x"],
    )
    assert all(picker.pick()[0] == "on" for _ in range(500))


def test_shipped_query_file_loads():
    templates = load_queries(ROOT / "queries" / "default.yaml")
    assert any(t["name"] == "keyword_search" for t in templates)


# --------------------------------------------------------------- loadtest ---

@pytest.fixture
async def mock_solr(aiohttp_server):
    async def select(request):
        await asyncio.sleep(0.005)
        return web.json_response({"response": {"numFound": 1, "docs": []}})

    async def slow_select(request):
        await asyncio.sleep(0.5)
        return web.json_response({"response": {"numFound": 1, "docs": []}})

    async def update(request):
        await asyncio.sleep(0.005)
        return web.json_response({"responseHeader": {"status": 0}})

    app = web.Application()
    app.router.add_get("/solr/test/select", select)
    app.router.add_get("/solr/slow/select", slow_select)
    app.router.add_post("/solr/test/update", update)
    return await aiohttp_server(app)


async def test_open_loop_rate_accuracy(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await run_load(base, "test", rps=100, duration=3, seed=1)
    achieved = len(result.records) / result.duration
    assert abs(achieved - 100) / 100 < 0.05  # within 5%
    assert all(r.ok for r in result.records)


async def test_mixed_load_includes_index_stream(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await run_load(base, "test", rps=30, duration=2, index_rps=10, seed=1)
    templates = {r.template for r in result.records}
    assert "_index" in templates
    idx = sum(1 for r in result.records if r.template == "_index")
    assert 12 <= idx <= 28  # ~20 expected


async def test_saturation_drops_instead_of_queueing(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    # 0.5s latency, cap 3 in flight, 50 rps schedule -> most must be dropped.
    result = await run_load(base, "slow", rps=50, duration=2, max_in_flight=3, seed=1)
    assert result.dropped > 50
    assert len(result.records) + result.dropped >= 80


async def test_ramp_increases_rate_over_time(mock_solr):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await run_load(base, "test", rps=80, duration=4, ramp=4, seed=1)
    tl = result.timeline(bucket_s=1)
    assert tl[0]["rps"] < tl[-1]["rps"]
    assert tl[-1]["rps"] > 50


# ---------------------------------------------------------------- reports ---

async def test_report_save_compare_html(mock_solr, tmp_path):
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    save_report(await run_load(base, "test", rps=40, duration=2, seed=1), a)
    save_report(await run_load(base, "test", rps=40, duration=2, seed=2), b)

    ra = load_report(a)
    assert ra["requests"] > 0 and "achieved_rps" in ra and ra["timeline"]

    text = compare_text(a, b)
    assert "p99 ms" in text and "delta" in text

    html_a = tmp_path / "a.html"
    html_report(a, html_a)
    content = html_a.read_text()
    assert "<svg" in content and "p99" in content

    cmp_html = tmp_path / "cmp.html"
    html_compare(a, b, cmp_html)
    assert "<svg" in cmp_html.read_text()
