"""Query builder: param construction, field discovery, response shaping."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab.query import build_params, list_fields, run_query


# ------------------------------------------------------------ build_params ---

def test_defaults_are_a_valid_match_all():
    p = build_params({})
    assert p["q"] == "*:*"
    assert p["rows"] == 10
    assert "defType" not in p          # lucene is Solr's default, not a param


def test_parser_and_qf_only_sent_for_dismax_family():
    p = build_params({"q": "boots", "parser": "edismax", "qf": "title_t^3 body_t"})
    assert p["defType"] == "edismax"
    assert p["qf"] == "title_t^3 body_t"
    # qf is meaningless under lucene, so it must not leak through
    p = build_params({"q": "boots", "parser": "lucene", "qf": "title_t^3"})
    assert "qf" not in p and "defType" not in p


def test_blank_inputs_are_dropped_not_sent_empty():
    p = build_params({"q": "  ", "sort": "  ", "fl": "", "fq": ["", "  "]})
    assert p["q"] == "*:*"             # blank query means match all
    assert "sort" not in p and "fl" not in p and "fq" not in p


def test_repeated_filters_are_preserved_as_a_list():
    p = build_params({"fq": ["a:1", " b:2 ", ""]})
    assert p["fq"] == ["a:1", "b:2"]   # repeated fq is meaningful in Solr


def test_faceting_switches_on_only_when_a_field_is_chosen():
    assert "facet" not in build_params({})
    p = build_params({"facet_fields": ["category_s"], "facet_limit": 5})
    assert p["facet"] == "true"
    assert p["facet.field"] == ["category_s"]
    assert p["facet.limit"] == 5
    assert p["facet.mincount"] == 1    # empty buckets are noise


def test_invalid_input_is_rejected_before_hitting_solr():
    with pytest.raises(ValueError, match="Parser"):
        build_params({"parser": "magic"})
    with pytest.raises(ValueError, match="Rows"):
        build_params({"rows": 5000})
    with pytest.raises(ValueError, match="Rows"):
        build_params({"rows": "many"})


# ------------------------------------------------------------ live-ish ---

@pytest.fixture
async def mock_solr(aiohttp_server):
    async def select(request):
        if request.rel_url.query.get("q") == "boom":
            return web.json_response({"error": {"msg": "undefined field nope"}},
                                     status=400)
        body = {
            "responseHeader": {"QTime": 7},
            "response": {"numFound": 42, "docs": [
                {"id": "a", "title_t": "One", "_version_": 1},
                {"id": "b", "price_f": 9.5, "_version_": 2},
            ]},
            "facet_counts": {"facet_fields": {"category_s": ["books", 12, "toys", 3]}},
        }
        if request.rel_url.query.get("debugQuery") == "true":
            body["debug"] = {"parsedquery_toString": "+title_t:boot"}
        return web.json_response(body)

    async def luke(request):
        return web.json_response({"fields": {
            "id": {"type": "string"},
            "title_t": {"type": "text_general"},
            "category_s": {"type": "string"},
            "_version_": {"type": "plong"},
        }})

    app = web.Application()
    app.router.add_get("/solr/products/select", select)
    app.router.add_get("/solr/products/admin/luke", luke)
    return await aiohttp_server(app)


async def test_list_fields_marks_text_and_hides_plumbing(mock_solr):
    spec = ClusterSpec(base_port=mock_solr.port)
    fields = await asyncio.to_thread(list_fields, spec, "products")
    names = [f["name"] for f in fields]
    assert "_version_" not in names          # internal, not a user field
    assert names == ["category_s", "id", "title_t"]
    by = {f["name"]: f["text"] for f in fields}
    assert by["title_t"] is True             # analyzed -> good for qf
    assert by["category_s"] is False         # exact -> good for faceting


async def test_run_query_flattens_facets_and_reports_url(mock_solr):
    spec = ClusterSpec(base_port=mock_solr.port)
    out = await asyncio.to_thread(run_query, spec, "products",
                                  {"q": "boots", "facet_fields": ["category_s"]})
    assert out["ok"] is True
    assert out["num_found"] == 42 and out["qtime"] == 7
    # Solr returns [value, count, value, count...]; the UI needs pairs
    assert out["facets"]["category_s"] == [
        {"value": "books", "count": 12}, {"value": "toys", "count": 3}]
    assert "/products/select" in out["url"]


async def test_explain_surfaces_the_parsed_query(mock_solr):
    spec = ClusterSpec(base_port=mock_solr.port)
    plain = await asyncio.to_thread(run_query, spec, "products", {"q": "boot"})
    assert "parsed" not in plain
    shown = await asyncio.to_thread(run_query, spec, "products",
                                    {"q": "boot", "explain": True})
    assert shown["parsed"] == "+title_t:boot"


async def test_solr_errors_come_back_readable_with_the_url(mock_solr):
    """A syntax error should explain itself, not surface as a raw 400."""
    spec = ClusterSpec(base_port=mock_solr.port)
    out = await asyncio.to_thread(run_query, spec, "products", {"q": "boom"})
    assert out["ok"] is False
    assert "undefined field nope" in out["error"]
    assert "select" in out["url"]
