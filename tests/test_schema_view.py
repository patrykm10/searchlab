"""Schema explorer: provenance of property values, on-disk flags, edits."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab.schema_view import (EDITABLE, FIELD_PROPS, _match_dynamic,
                                   parse_field_flags, read_schema,
                                   set_field_property)

HTML = (Path(__file__).parent.parent / "searchlab" / "templates" / "dashboard.html").read_text()


# ---- decoding what a segment physically holds ----------------------------

def test_string_with_sorted_docvalues():
    f = parse_field_flags("IDsrt-OF----")
    assert f["indexed"] and f["docValues"] == "SORTED"
    assert f["omitNorms"] and f["omitTermFreqAndPositions"] and not f["termVectors"]
    assert f["points"] is None


def test_point_field_has_no_postings_but_points():
    f = parse_field_flags("-Dnum-------:1:1:4")
    assert not f["indexed"] and f["docValues"] == "NUMERIC"
    assert f["points"] == {"dims": 1, "bytes": 4}


def test_text_field_without_docvalues():
    f = parse_field_flags("I-----------")
    assert f["indexed"] and f["docValues"] is None and not f["omitNorms"]


def test_multivalued_strings_are_sorted_set():
    assert parse_field_flags("IDsrs-OF----")["docValues"] == "SORTED_SET"


# ---- which dynamic rule an undeclared field comes from --------------------

def test_longest_matching_pattern_wins():
    assert _match_dynamic("price_f", ["*_f", "*_s"]) == "*_f"
    assert _match_dynamic("attr_color_s", ["*_s", "attr_*", "*_color_s"]) == "*_color_s"
    assert _match_dynamic("nothing", ["*_s"]) is None


# ---- a fake Solr for the whole read --------------------------------------

DECLARED = {"schema": {
    "name": "default-config", "version": 1.6, "uniqueKey": "id",
    "fieldTypes": [
        {"name": "string", "class": "solr.StrField", "sortMissingLast": True, "docValues": True},
        {"name": "text_general", "class": "solr.TextField", "positionIncrementGap": "100",
         "indexAnalyzer": {"tokenizer": {"name": "standard"}, "filters": [{"name": "lowercase"}]},
         "queryAnalyzer": {"tokenizer": {"name": "standard"},
                           "filters": [{"name": "synonymGraph"}, {"name": "lowercase"}]}},
    ],
    "fields": [{"name": "id", "type": "string", "indexed": True, "stored": True, "required": True}],
    "dynamicFields": [{"name": "*_s", "type": "string", "indexed": True, "stored": True},
                      {"name": "*_t", "type": "text_general", "indexed": True, "stored": True}],
    "copyFields": [],
}}
EFFECTIVE_STRING = {"indexed": True, "stored": True, "docValues": True, "multiValued": False,
                    "omitNorms": True, "sortMissingLast": True, "tokenized": False}


@pytest.fixture
async def fake_solr(aiohttp_server):
    seen = {}

    async def schema(request):
        return web.json_response(DECLARED)

    async def fields(request):
        return web.json_response({"fields": [
            {"name": "id", "type": "string", **EFFECTIVE_STRING, "required": True}]})

    async def dynamic(request):
        return web.json_response({"dynamicFields": [
            {"name": "*_s", "type": "string", **EFFECTIVE_STRING},
            {"name": "*_t", "type": "text_general", "indexed": True, "stored": True,
             "docValues": False, "omitNorms": False, "tokenized": True}]})

    async def fieldtypes(request):
        return web.json_response({"fieldTypes": [
            {"name": "string", "class": "solr.StrField", **EFFECTIVE_STRING},
            {"name": "text_general", "class": "solr.TextField", "positionIncrementGap": "100",
             "indexAnalyzer": DECLARED["schema"]["fieldTypes"][1]["indexAnalyzer"],
             "queryAnalyzer": DECLARED["schema"]["fieldTypes"][1]["queryAnalyzer"]}]})

    async def one_type(request):
        return web.json_response({"fieldType": {"name": "string", **EFFECTIVE_STRING}})

    async def one_dynamic(request):
        return web.json_response({"dynamicField": DECLARED["schema"]["dynamicFields"][0]})

    async def post_schema(request):
        seen["posted"] = await request.json()
        return web.json_response({"responseHeader": {"status": 0}})

    async def luke(request):
        return web.json_response({"fields": {
            "id": {"type": "string", "docs": 3},
            "color_s": {"type": "string", "dynamicBase": "*_s", "docs": 3},
            "title_t": {"type": "text_general", "dynamicBase": "*_t", "docs": 3}}})

    async def segments(request):
        return web.json_response({"segments": {"_0": {"fields": {
            "color_s": {"flags": "IDsrt-OF----", "docCount": 3, "termCount": 2}}}}})

    async def clusterstatus(request):
        return web.json_response({"cluster": {"collections": {
            "products": {"configName": "searchlab"},
            "other": {"configName": "searchlab"},
            "solo": {"configName": "_default"}}}})

    app = web.Application()
    r = app.router
    r.add_get("/solr/products/schema", schema)
    r.add_post("/solr/products/schema", post_schema)
    r.add_get("/solr/products/schema/fields", fields)
    r.add_get("/solr/products/schema/dynamicfields", dynamic)
    r.add_get("/solr/products/schema/dynamicfields/{name}", one_dynamic)
    r.add_get("/solr/products/schema/fieldtypes", fieldtypes)
    r.add_get("/solr/products/schema/fieldtypes/{name}", one_type)
    r.add_get("/solr/products/admin/luke", luke)
    r.add_get("/solr/products/admin/segments", segments)
    r.add_get("/solr/admin/collections", clusterstatus)
    server = await aiohttp_server(app)
    server.seen = seen
    return server


async def test_each_value_says_which_layer_it_came_from(fake_solr):
    s = await asyncio.to_thread(read_schema, ClusterSpec(base_port=fake_solr.port), "products")
    color = next(f for f in s["in_index"] if f["name"] == "color_s")
    assert color["source"] == {"kind": "dynamic", "name": "*_s"}
    assert color["props"]["stored"] == {"value": True, "from": "field"}       # the *_s rule
    assert color["props"]["docValues"] == {"value": True, "from": "type"}     # string type
    assert color["props"]["omitNorms"] == {"value": True, "from": "default"}  # StrField itself


async def test_on_disk_state_and_rule_matches(fake_solr):
    s = await asyncio.to_thread(read_schema, ClusterSpec(base_port=fake_solr.port), "products")
    color = next(f for f in s["in_index"] if f["name"] == "color_s")
    assert color["segments"][0]["docValues"] == "SORTED"
    rules = {d["name"]: d["matches"] for d in s["dynamic_fields"]}
    assert rules == {"*_s": ["color_s"], "*_t": ["title_t"]}
    types = {t["name"]: t for t in s["field_types"]}
    assert types["text_general"]["in_use"] and types["text_general"]["used_by"] == ["title_t"]
    assert set(types["text_general"]["analyzers"]) == {"indexAnalyzer", "queryAnalyzer"}


async def test_shared_configset_is_named(fake_solr):
    s = await asyncio.to_thread(read_schema, ClusterSpec(base_port=fake_solr.port), "products")
    assert s["configset"] == "searchlab" and s["shared_with"] == ["other"]


async def test_edit_replaces_the_declared_definition_not_the_effective_one(fake_solr):
    spec = ClusterSpec(base_port=fake_solr.port)
    out = await asyncio.to_thread(set_field_property, spec, "products", "dynamic", "*_s",
                                  "stored", False, True)
    # inherited defaults are not pinned into the schema
    assert out["command"] == {"replace-dynamic-field":
                              {"name": "*_s", "type": "string", "indexed": True, "stored": False}}
    assert "posted" not in fake_solr.seen            # dry run sends nothing


async def test_setting_the_inherited_value_removes_the_override(fake_solr):
    spec = ClusterSpec(base_port=fake_solr.port)
    out = await asyncio.to_thread(set_field_property, spec, "products", "dynamic", "*_s",
                                  "docValues", True)
    assert "docValues" not in out["command"]["replace-dynamic-field"]
    assert fake_solr.seen["posted"] == out["command"]


def test_only_real_toggles_can_be_edited():
    with pytest.raises(ValueError):
        set_field_property(ClusterSpec(), "products", "field", "id", "tokenized", True)
    with pytest.raises(ValueError):
        set_field_property(ClusterSpec(), "products", "type", "string", "stored", True)


# ---- the page explains everything the backend can show -------------------

HELP_KEYS = set(re.findall(r'^\s*"((?:attr|top|class|special|tokenizer|filter|charFilter|param):[^"]+)":',
                           HTML, re.M))


def test_every_field_property_has_an_explanation():
    missing = [p for p in FIELD_PROPS if f"attr:{p}" not in HELP_KEYS]
    assert not missing, missing


def test_every_editable_property_says_what_changing_it_does():
    block = HTML[HTML.index("const SX_FLIP_OF"):HTML.index("function sxFlip")]
    covered = set(re.findall(r"(\w+):", block)) | {"multiValued"}
    assert EDITABLE <= covered, EDITABLE - covered


# ---- every shard, not whichever replica answered ----------------------------

def test_one_replica_per_shard_prefers_the_leader_and_skips_the_inactive():
    from searchlab.schema_view import one_replica_per_shard
    state = {"shards": {
        "shard2": {"state": "active", "replicas": {
            "r3": {"core": "c_s2_n3", "node_name": "solr2:8983_solr", "state": "active"},
            "r4": {"core": "c_s2_n4", "node_name": "solr3:8983_solr", "state": "active",
                   "leader": "true"}}},
        "shard1": {"state": "active", "replicas": {
            "r1": {"core": "c_s1_n1", "node_name": "solr1:8983_solr", "state": "down",
                   "leader": "true"},
            "r2": {"core": "c_s1_n2", "node_name": "solr2:8983_solr", "state": "active"}}},
        "shard3": {"state": "inactive", "replicas": {
            "r5": {"core": "c_s3", "node_name": "solr1:8983_solr", "state": "active"}}}}}
    # shard1's leader is down, so its live replica answers; shard3 was split away
    assert one_replica_per_shard(state) == [("shard1", "c_s1_n2", 1), ("shard2", "c_s2_n4", 2)]


@pytest.fixture
async def two_shard_solr(aiohttp_server):
    async def empty_schema(request):
        return web.json_response({"schema": {"name": "s", "fields": [], "dynamicFields": [
            {"name": "*_s", "type": "string"}], "fieldTypes": []}})

    async def eff(key):
        async def h(request):
            return web.json_response({key: [{"name": "*_s", "type": "string"}]
                                      if key == "dynamicFields" else []})
        return h

    def luke(docs):
        async def h(request):
            return web.json_response({"fields": {
                "color_s": {"type": "string", "dynamicBase": "*_s", "docs": docs}}})
        return h

    def segments(count):
        async def h(request):
            return web.json_response({"segments": {"_0": {"fields": {
                "color_s": {"flags": "IDsrt-OF----", "docCount": count, "termCount": 2}}}}})
        return h

    async def clusterstatus(request):
        return web.json_response({"cluster": {"collections": {"products": {
            "configName": "searchlab", "shards": {
                "shard1": {"state": "active", "replicas": {"r1": {
                    "core": "products_shard1_replica_n1", "node_name": "solr1:8983_solr",
                    "state": "active", "leader": "true"}}},
                "shard2": {"state": "active", "replicas": {"r2": {
                    "core": "products_shard2_replica_n2", "node_name": "solr1:8983_solr",
                    "state": "active", "leader": "true"}}}}}}}})

    app = web.Application()
    r = app.router
    r.add_get("/solr/products/schema", empty_schema)
    r.add_get("/solr/products/schema/fields", await eff("fields"))
    r.add_get("/solr/products/schema/dynamicfields", await eff("dynamicFields"))
    r.add_get("/solr/products/schema/fieldtypes", await eff("fieldTypes"))
    r.add_get("/solr/products_shard1_replica_n1/admin/luke", luke(7))
    r.add_get("/solr/products_shard2_replica_n2/admin/luke", luke(5))
    r.add_get("/solr/products_shard1_replica_n1/admin/segments", segments(7))
    r.add_get("/solr/products_shard2_replica_n2/admin/segments", segments(5))
    r.add_get("/solr/admin/collections", clusterstatus)
    return await aiohttp_server(app)


async def test_in_your_data_covers_every_shard(two_shard_solr):
    s = await asyncio.to_thread(read_schema, ClusterSpec(base_port=two_shard_solr.port), "products")
    color = next(f for f in s["in_index"] if f["name"] == "color_s")
    assert color["docs"] == 12                      # 7 + 5, not whichever shard answered
    assert [g["segment"] for g in color["segments"]] == ["shard1 _0", "shard2 _0"]
    assert s["read_from"] == ["products_shard1_replica_n1", "products_shard2_replica_n2"]


def test_edits_go_to_the_collection_they_were_previewed_for():
    page = (Path(__file__).parent.parent / "searchlab" / "templates" / "dashboard.html").read_text()
    send = page[page.index("async function sxSend"):page.index("async function sxSend") + 400]
    assert "collection: p.collection" in send and "coll()" not in send
    cf = page[page.index("async function cfSend"):page.index("async function cfSend") + 400]
    assert "collection: e.collection" in cf and "coll()" not in cf.split("loadTuning")[0]
    # a pending preview is dropped when the collection changes
    assert "if (schemaFor !== c) sxPending = null;" in page
    assert "if (configFor !== c) cfEditing = null;" in page
