"""Vector search support: data generation, RAND_VECTOR, schema, kNN load."""

from __future__ import annotations

import asyncio
import math
import random

import pytest
from aiohttp import web

from solrlab.datagen import generate
from solrlab.loadtest import QueryPicker, run_load
from solrlab.schema import fields_from_profile, mappings_from_profile, vector_field_types

VEC_PROFILE = {"fields": {
    "id": {"type": "id"},
    "vec": {"type": "vector", "dims": 16, "clusters": 4, "cluster_std": 0.1,
            "similarity": "cosine",
            "solr": {"hnswMaxConnections": 24, "hnswBeamWidth": 200}},
}}


# ---------------------------------------------------------------- datagen ---

def test_vector_generation_shape_and_norm():
    docs = list(generate(VEC_PROFILE, 50, seed=3))
    for d in docs:
        v = d["vec"]
        assert len(v) == 16
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0, abs=0.01)


def test_vector_generation_deterministic_and_clustered():
    a = list(generate(VEC_PROFILE, 200, seed=5))
    b = list(generate(VEC_PROFILE, 200, seed=5))
    assert a == b
    # clustered: docs sharing a centroid should be far more similar than the
    # cross-cluster average — check the max pairwise cosine is near 1
    vecs = [d["vec"] for d in a[:60]]
    sims = [sum(x * y for x, y in zip(u, v, strict=True))
            for i, u in enumerate(vecs) for v in vecs[i + 1:]]
    assert max(sims) > 0.9      # same-cluster neighbors exist
    assert min(sims) < 0.5      # distinct clusters exist


# ----------------------------------------------------------- substitution ---

def test_rand_vector_exact_becomes_list():
    picker = QueryPicker(
        [{"name": "knn", "weight": 1,
          "body": {"knn": {"field": "vec", "query_vector": "{RAND_VECTOR:8}", "k": 10}}}],
        random.Random(0), ["x"])
    t = picker.pick_template()
    qv = t["body"]["knn"]["query_vector"]
    assert isinstance(qv, list) and len(qv) == 8
    assert math.sqrt(sum(x * x for x in qv)) == pytest.approx(1.0, abs=0.01)


def test_rand_vector_embedded_becomes_bracket_text():
    picker = QueryPicker(
        [{"name": "knn", "weight": 1,
          "params": {"q": "{!knn f=vec topK=10}{RAND_VECTOR:4}"}}],
        random.Random(0), ["x"])
    _, params = picker.pick()
    q = params["q"]
    assert q.startswith("{!knn f=vec topK=10}[") and q.endswith("]")
    nums = [float(x) for x in q.split("}[")[1][:-1].split(", ")]
    assert len(nums) == 4


def test_shipped_vector_templates_load():
    from pathlib import Path

    from solrlab.loadtest import load_queries
    root = Path(__file__).parent.parent / "queries"
    for f in ("vector-solr.yaml", "vector-es.yaml", "vector-os.yaml"):
        assert load_queries(root / f)


# ------------------------------------------------------------------ schema ---

def test_solr_vector_field_types_and_fields():
    types = vector_field_types(VEC_PROFILE)
    assert len(types) == 1
    t = types[0]
    assert t["class"] == "solr.DenseVectorField"
    assert t["vectorDimension"] == 16 and t["similarityFunction"] == "cosine"
    assert t["hnswMaxConnections"] == 24 and t["hnswBeamWidth"] == 200

    fields = {f["name"]: f for f in fields_from_profile(VEC_PROFILE)}
    assert fields["vec"]["type"] == "knn_vector_16_cosine"
    assert "hnswMaxConnections" not in fields["vec"]  # knob lives on the type


def test_es_vs_os_vector_mappings():
    es = mappings_from_profile(VEC_PROFILE, engine="elasticsearch")["properties"]["vec"]
    assert es == {"type": "dense_vector", "dims": 16, "index": True,
                  "similarity": "cosine"}
    os_ = mappings_from_profile(VEC_PROFILE, engine="opensearch")["properties"]["vec"]
    assert os_["type"] == "knn_vector" and os_["dimension"] == 16
    assert os_["method"]["space_type"] == "cosinesimil"


def test_os_index_gets_knn_setting():
    # bulk shape sanity only; the create-index knn flag is asserted via the
    # settings dict the engine builds
    import solrlab.engines as E
    from solrlab.engines import get_engine
    captured = {}

    class FakeResp:
        status_code = 200

    def fake_put(url, json, timeout):
        captured.update(json["settings"])
        return FakeResp()

    monkey = E.httpx.put
    E.httpx.put = fake_put
    try:
        get_engine("opensearch").create_index(
            type("S", (), {"base_port": 9200, "base_url": lambda self, n=0: "http://x"})(),
            "idx", 1, 1)
    finally:
        E.httpx.put = monkey
    assert captured["index.knn"] is True


# --------------------------------------------------------------- kNN load ---

@pytest.fixture
async def mock_es(aiohttp_server):
    seen = []

    async def search(request):
        seen.append(await request.json())
        await asyncio.sleep(0.002)
        return web.json_response({"hits": {"hits": []}})

    app = web.Application()
    app.router.add_post("/idx/_search", search)
    server = await aiohttp_server(app)
    server.seen = seen
    return server


async def test_knn_load_against_mock_es(mock_es, tmp_path):
    from pathlib import Path
    q = Path(__file__).parent.parent / "queries" / "vector-es.yaml"
    base = f"http://{mock_es.host}:{mock_es.port}"
    result = await run_load(base, "idx", rps=30, duration=1.5, seed=1,
                            queries_path=q, engine="elasticsearch")
    assert all(r.ok for r in result.records) and len(result.records) > 20
    body = mock_es.seen[0]
    assert isinstance(body["knn"]["query_vector"], list)
    assert len(body["knn"]["query_vector"]) == 384
    # vectors differ per request (cache-busting works for kNN too)
    assert mock_es.seen[0]["knn"]["query_vector"] != mock_es.seen[1]["knn"]["query_vector"]
