"""Embeddings and semantic search.

These use a stub embedder rather than a real model — the suite must not
download ~90 MB of weights to check that a kNN query is shaped correctly.
"""

from __future__ import annotations

import pytest

from searchlab.embeddings import MODELS, knn_query, vector_profile
from searchlab.query import build_params


class StubEmbedder:
    """Deterministic stand-in with the interface build_params relies on."""

    dims = 4

    def embed(self, texts):
        return [[float(len(t)), 0.5, -0.25, 1.0] for t in texts]

    def embed_one(self, text):
        return self.embed([text])[0]


# ------------------------------------------------------------- registry ---

def test_model_registry_declares_dimensions():
    """Dimensions must be known before indexing: a DenseVectorField is
    declared with a fixed size and rejects vectors of any other length."""
    for name, (model_id, dims) in MODELS.items():
        assert isinstance(dims, int) and dims > 0, name
        assert "/" in model_id, name
    assert MODELS["minilm"][1] == 384          # matches profiles/vectors.yaml


def test_vector_profile_matches_what_schema_expects():
    p = vector_profile(384, "cosine", "vec")
    field = p["fields"]["vec"]
    assert field["type"] == "vector"
    assert field["dims"] == 384 and field["similarity"] == "cosine"


# ---------------------------------------------------------- knn syntax ---

def test_knn_query_uses_solr_local_params_syntax():
    q = knn_query("vec", [0.1, -0.2, 0.3], top_k=25)
    assert q.startswith("{!knn f=vec topK=25}[")
    assert q.endswith("]")
    assert "0.100000,-0.200000,0.300000" in q


# ------------------------------------------------------ semantic search ---

def test_semantic_query_embeds_the_text_and_becomes_knn():
    p = build_params({"q": "quiet mouse", "semantic": True, "rows": 5},
                     embedder=StubEmbedder())
    assert p["q"].startswith("{!knn f=vec topK=5}")
    # the embedded text, not the text itself
    assert "quiet mouse" not in p["q"]
    assert "11.000000" in p["q"]        # len("quiet mouse") from the stub
    assert "defType" not in p           # {!knn} is local-params, not a parser


def test_semantic_respects_vector_field_and_top_k():
    p = build_params({"q": "hi", "semantic": True, "vector_field": "embedding",
                      "top_k": 42}, embedder=StubEmbedder())
    assert p["q"].startswith("{!knn f=embedding topK=42}")


def test_semantic_keeps_filters_so_prefiltered_knn_still_works():
    p = build_params({"q": "hi", "semantic": True, "fq": ["category_s:books"]},
                     embedder=StubEmbedder())
    assert p["fq"] == ["category_s:books"]


def test_semantic_without_a_model_is_refused_not_silently_keyword():
    """Falling back to keyword search would quietly answer a different
    question than the one asked."""
    with pytest.raises(ValueError, match="model"):
        build_params({"q": "hi", "semantic": True}, embedder=None)


def test_semantic_needs_something_to_search_for():
    with pytest.raises(ValueError, match="something to search for"):
        build_params({"q": "", "semantic": True}, embedder=StubEmbedder())


def test_keyword_path_is_untouched_by_the_semantic_option():
    p = build_params({"q": "body_t:merge", "parser": "edismax", "qf": "title_t^3"})
    assert p["q"] == "body_t:merge"
    assert p["defType"] == "edismax"
