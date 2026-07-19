"""Recall measurement tests: exact ground truth, mock engines, the curve."""

from __future__ import annotations

import json

import numpy as np
import pytest
from aiohttp import web

from solrlab.datagen import generate
from solrlab.recall import (
    find_vector_field,
    gen_query_vectors,
    ground_truth,
    load_vectors,
    run_recall,
)

PROFILE = {"fields": {
    "id": {"type": "id"},
    "vec": {"type": "vector", "dims": 8, "clusters": 3, "cluster_std": 0.05},
}}


def _dataset(tmp_path, n=60, seed=2):
    p = tmp_path / "d.jsonl"
    with open(p, "w") as f:
        for doc in generate(PROFILE, n, seed=seed):
            f.write(json.dumps(doc) + "\n")
    return p


# ------------------------------------------------------------ ground truth ---

def test_ground_truth_is_exact():
    # hand-built case: 4 orthogonal-ish vectors, query equals vector 2
    data = np.eye(4, dtype="float32")
    queries = np.asarray([[0, 0, 1, 0]], dtype="float32")
    truth = ground_truth(data, queries, k=1)
    assert truth == [{2}]
    truth2 = ground_truth(data, queries, k=2)
    assert 2 in truth2[0] and len(truth2[0]) == 2


def test_load_vectors_and_field_detection(tmp_path):
    field, cfg = find_vector_field(PROFILE)
    assert field == "vec" and cfg["dims"] == 8
    ids, data = load_vectors(_dataset(tmp_path), "vec")
    assert len(ids) == 60 and data.shape == (60, 8)
    assert np.allclose(np.linalg.norm(data, axis=1), 1.0, atol=0.01)


def test_query_vectors_in_distribution_but_unseen():
    qs = gen_query_vectors(PROFILE["fields"]["vec"], 20, seed=99)
    assert qs.shape == (20, 8)
    qs2 = gen_query_vectors(PROFILE["fields"]["vec"], 20, seed=99)
    assert np.array_equal(qs, qs2)  # seeded


# ------------------------------------------------------------ mock engines ---

def _mock_es_app(ids, data, degrade=0):
    """Mock ES whose kNN is exact — or returns `degrade` wrong ids."""
    async def search(request):
        body = await request.json()
        q = np.asarray(body["knn"]["query_vector"], dtype="float32")
        k = body["knn"]["k"]
        top = np.argsort(-(data @ q))[:k].tolist()
        got = [ids[i] for i in top]
        if degrade:
            got = got[:-degrade] + [f"wrong{j}" for j in range(degrade)]
        return web.json_response({"hits": {"hits": [{"_id": g} for g in got]}})

    app = web.Application()
    app.router.add_post("/vecs/_search", search)
    return app


async def test_recall_perfect_engine(aiohttp_server, tmp_path):
    ids, data = load_vectors(_dataset(tmp_path), "vec")
    server = await aiohttp_server(_mock_es_app(ids, data))
    qs = gen_query_vectors(PROFILE["fields"]["vec"], 25, seed=7)
    truth = ground_truth(data, qs, k=5)
    r = await run_recall("elasticsearch", f"http://{server.host}:{server.port}",
                         "vecs", "vec", ids, qs, truth, k=5, candidates=50)
    assert r["recall_mean"] == 1.0
    assert r["recall_min"] == 1.0
    assert r["errors"] == 0
    assert r["lat_p50_ms"] is not None


async def test_recall_degraded_engine(aiohttp_server, tmp_path):
    ids, data = load_vectors(_dataset(tmp_path), "vec")
    server = await aiohttp_server(_mock_es_app(ids, data, degrade=2))
    qs = gen_query_vectors(PROFILE["fields"]["vec"], 25, seed=7)
    truth = ground_truth(data, qs, k=5)
    r = await run_recall("elasticsearch", f"http://{server.host}:{server.port}",
                         "vecs", "vec", ids, qs, truth, k=5)
    # 2 of 5 replaced with garbage -> recall exactly 0.6
    assert r["recall_mean"] == pytest.approx(0.6, abs=0.01)


async def test_recall_solr_request_shape(aiohttp_server, tmp_path):
    seen = {}

    async def select(request):
        seen.update(request.query)
        return web.json_response({"response": {"docs": [{"id": "doc-0"}]}})

    app = web.Application()
    app.router.add_get("/solr/vecs/select", select)
    server = await aiohttp_server(app)
    ids, data = load_vectors(_dataset(tmp_path), "vec")
    qs = gen_query_vectors(PROFILE["fields"]["vec"], 2, seed=7)
    truth = ground_truth(data, qs, k=3)
    await run_recall("solr", f"http://{server.host}:{server.port}/solr",
                     "vecs", "vec", ids, qs, truth, k=3)
    assert seen["q"].startswith("{!knn f=vec topK=3}[")
    assert seen["fl"] == "id" and seen["rows"] == "3"
