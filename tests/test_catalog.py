"""The catalogue corpus and the relevance comparison it exists to support.

The point of this corpus is that a query can be *wrong* about it, which the
random-wordlist profiles cannot support. These guard the properties that
claim rests on: documents that hold together, categories that are real
ground truth, and queries phrased the way someone would ask rather than
copied out of the documents they are supposed to find.
"""

from __future__ import annotations

import random

from searchlab.catalog import CATEGORIES, benchmark_queries, product_doc
from searchlab.datagen import generate, load_profile
from searchlab.relevance import _precision_at_k, format_report


def docs(n, seed=1):
    rng = random.Random(seed)
    return [product_doc(i, rng) for i in range(n)]


def test_a_document_agrees_with_itself():
    """Title, body and category are one statement or the corpus is no better
    than the random one it replaces."""
    for d in docs(60):
        cat = next(c for c in CATEGORIES if c["slug"] == d["category_s"])
        assert cat["noun"] in d["title_t"]
        assert d["title_t"] in d["body_t"]
        assert d["department_s"] == cat["dept"]


def test_price_follows_the_category_not_the_catalogue():
    """A road bike and a cast iron pan should not be drawn from one range,
    or a price filter selects a random slice rather than a plausible one."""
    for d in docs(200):
        lo, hi = next(c["price"] for c in CATEGORIES if c["slug"] == d["category_s"])
        assert lo <= d["price_f"] <= hi


def test_the_corpus_is_not_all_one_thing():
    seen = {d["category_s"] for d in docs(400)}
    assert len(seen) >= len(CATEGORIES) - 1


def test_generation_is_reproducible_for_a_seed():
    assert docs(20, seed=5) == docs(20, seed=5)
    assert docs(20, seed=5) != docs(20, seed=6)


def test_every_query_names_a_category_that_exists():
    """A query whose ground truth points at nothing would score zero forever
    and look like a retrieval failure."""
    slugs = {c["slug"] for c in CATEGORIES}
    cases = benchmark_queries()
    assert cases
    for case in cases:
        assert case["relevant"]
        for slug in case["relevant"]:
            assert slug in slugs


def test_queries_do_not_quote_the_documents_they_should_find():
    """These are meant to be asked in a shopper's words, not the seller's.
    A query that shares the product's own noun is one term matching already
    handles, and proves nothing about embeddings."""
    for cat in CATEGORIES:
        for phrase in cat["sells_as"]:
            assert cat["noun"].lower() not in phrase.lower(), (
                f"{cat['slug']}: {phrase!r} contains its own product noun")


def test_catalog_profile_layers_declared_fields_on_top():
    """The catalogue owns the fields that have to agree; the profile still
    supplies the ones it has no opinion about."""
    profile = load_profile("profiles/catalog.yaml")
    doc = next(iter(generate(profile, 1, seed=3)))
    assert doc["category_s"] and doc["title_t"]
    assert "created_dt" in doc          # from the profile, not the catalogue


def test_precision_counts_only_what_came_back():
    assert _precision_at_k(["a", "a", "b", "c"], ["a"]) == 0.5
    assert _precision_at_k([], ["a"]) == 0.0      # nothing returned is not perfect
    assert _precision_at_k(["a", "a"], ["a"]) == 1.0


def test_report_names_the_queries_term_matching_won():
    """Reporting only the wins would make the corpus an advert rather than a
    measurement."""
    result = {
        "k": 10, "queries": 2,
        "lexical_mean_p_at_k": 0.4, "semantic_mean_p_at_k": 0.7,
        "lexical_empty_results": 1,
        "lexical_median_ms": 3.0, "semantic_median_ms": 9.0,
        "rows": [
            {"query": "warm coat", "lexical_p_at_k": 0.9, "semantic_p_at_k": 0.2,
             "relevant": ["winter-jackets"], "lexical_ms": 3, "semantic_ms": 9,
             "lexical_empty": False},
            {"query": "block out noise", "lexical_p_at_k": 0.0,
             "semantic_p_at_k": 1.0, "relevant": ["headphones"],
             "lexical_ms": 3, "semantic_ms": 9, "lexical_empty": True},
        ],
    }
    text = format_report(result)
    assert "Queries where term matching did better" in text
    assert "warm coat" in text
    assert "returned nothing at all" in text
