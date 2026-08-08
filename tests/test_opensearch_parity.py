"""OpenSearch/Elasticsearch equivalents of the Solr-side features.

The knobs and query builder are the same idea pointed at a different API,
so these check the translation rather than re-testing the concepts.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab import query_os, tuning_os


class StubEmbedder:
    dims = 4

    def embed_one(self, text):
        return [float(len(text)), 0.5, -0.25, 1.0]

    def embed(self, texts):
        return [self.embed_one(t) for t in texts]


# --------------------------------------------------------------- tuning ---

def test_every_knob_names_a_real_index_setting():
    for name, knob in tuning_os.KNOBS.items():
        assert knob["setting"].startswith("index."), name
        assert knob["setting"].endswith(knob["path"]), name
        assert knob["kind"] in ("seconds", "size_mb", "number"), name


def test_registry_strips_wiring_but_keeps_the_setting_name():
    reg = tuning_os.registry()
    for knob in reg.values():
        assert "path" not in knob and "kind" not in knob
        assert {"label", "desc", "unit", "min", "max", "default",
                "setting"} <= set(knob)


@pytest.mark.parametrize("raw,expected", [
    ("512mb", 512.0),
    ("5368709120b", 5120.0),      # the notation OS actually returns for 5gb
    ("1gb", 1024.0),
    (2097152, 2.0),               # a bare byte count
    (None, None),
])
def test_size_values_arrive_in_several_notations(raw, expected):
    assert tuning_os._to_mb(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("30s", 30.0), ("1m", 60.0), ("500ms", 0.5), ("-1", None),
])
def test_duration_values_arrive_in_several_notations(raw, expected):
    """-1 means refresh disabled, which is not a number to show as a value."""
    assert tuning_os._to_seconds(raw) == expected


def test_values_are_written_back_in_the_notation_the_api_expects():
    assert tuning_os._write("seconds", 30) == "30s"
    assert tuning_os._write("size_mb", 512) == "512mb"
    assert tuning_os._write("number", 4.0) == 4        # not 4.0


@pytest.fixture
async def mock_os(aiohttp_server):
    state = {"put": None}

    async def settings(request):
        return web.json_response({"shop": {
            "settings": {"index.refresh_interval": "30s",
                         "index.merge.policy.segments_per_tier": "4"},
            "defaults": {"index.translog.flush_threshold_size": "512mb",
                         "index.merge.policy.max_merged_segment": "5368709120b",
                         "index.number_of_replicas": "1",
                         "index.merge.policy.deletes_pct_allowed": "20.0",
                         "index.merge.scheduler.max_thread_count": "4",
                         "index.max_result_window": "10000"},
        }})

    async def put_settings(request):
        state["put"] = await request.json()
        return web.json_response({"acknowledged": True})

    async def mapping(request):
        return web.json_response({"shop": {"mappings": {"properties": {
            "title": {"type": "text",
                      "fields": {"keyword": {"type": "keyword"}}},
            "price": {"type": "float"},
        }}}})

    app = web.Application()
    app.router.add_get("/shop/_settings", settings)
    app.router.add_put("/shop/_settings", put_settings)
    app.router.add_get("/shop/_mapping", mapping)
    server = await aiohttp_server(app)
    server.state = state
    return server


async def test_defaults_fill_in_settings_never_explicitly_set(mock_os):
    """A setting left at its default is absent from the index's own block;
    showing it blank would imply it was unset rather than defaulted."""
    spec = ClusterSpec(engine="opensearch", base_port=mock_os.port)
    values = (await asyncio.to_thread(tuning_os.tuning_state, spec, "shop"))["values"]
    assert values["refresh_s"] == 30.0            # explicitly set
    assert values["translog_mb"] == 512.0         # from defaults
    assert values["max_merged_mb"] == 5120.0      # byte notation, converted
    assert values["segments_per_tier"] == 4.0


async def test_applying_a_knob_puts_the_right_setting(mock_os):
    spec = ClusterSpec(engine="opensearch", base_port=mock_os.port)
    await asyncio.to_thread(tuning_os.apply_tuning, spec, "shop", "refresh_s", 15)
    assert mock_os.state["put"] == {"index": {"refresh_interval": "15s"}}


async def test_knob_ranges_are_enforced_before_any_request(mock_os):
    spec = ClusterSpec(engine="opensearch", base_port=mock_os.port)
    with pytest.raises(ValueError, match="between"):
        await asyncio.to_thread(tuning_os.apply_tuning, spec, "shop",
                                "segments_per_tier", 999)
    with pytest.raises(ValueError, match="Unknown setting"):
        await asyncio.to_thread(tuning_os.apply_tuning, spec, "shop", "nope", 1)
    assert mock_os.state["put"] is None


async def test_keyword_subfields_are_offered_because_facets_need_them(mock_os):
    """Aggregating a text field is an error in ES/OS; the .keyword subfield
    is the thing that actually works, so it has to be discoverable."""
    spec = ClusterSpec(engine="opensearch", base_port=mock_os.port)
    fields = await asyncio.to_thread(query_os.list_fields, spec, "shop")
    by = {f["name"]: f for f in fields}
    assert by["title"]["text"] is True             # good for searching
    assert by["title.keyword"]["text"] is False    # good for faceting
    assert by["price"]["text"] is False


# ---------------------------------------------------------- query shapes ---

def test_lucene_parser_becomes_query_string():
    body = query_os.build_body({"q": "title:mouse", "rows": 5})
    assert body["query"] == {"query_string": {"query": "title:mouse"}}
    assert body["size"] == 5


def test_blank_query_becomes_match_all_not_an_empty_string():
    assert query_os.build_body({"q": ""})["query"] == {"match_all": {}}
    assert query_os.build_body({"q": "*:*"})["query"] == {"match_all": {}}


def test_edismax_becomes_multi_match_carrying_the_field_boosts():
    body = query_os.build_body({"q": "mouse", "parser": "edismax",
                                "qf": "title^3 body"})
    assert body["query"] == {
        "multi_match": {"query": "mouse", "fields": ["title^3", "body"]}}


def test_filters_become_a_non_scoring_bool_filter():
    """fq in Solr does not affect scoring; the filter clause is the
    equivalent, as opposed to must."""
    body = query_os.build_body({"q": "mouse", "fq": ["price:[1 TO 5]", "  "]})
    assert body["query"]["bool"]["filter"] == [
        {"query_string": {"query": "price:[1 TO 5]"}}]
    assert body["query"]["bool"]["must"] == [{"query_string": {"query": "mouse"}}]


def test_facets_become_terms_aggregations():
    body = query_os.build_body({"facet_fields": ["cat.keyword"], "facet_limit": 5})
    assert body["aggs"] == {"cat.keyword": {"terms": {"field": "cat.keyword",
                                                       "size": 5}}}


def test_solr_style_sort_is_translated():
    body = query_os.build_body({"sort": "price desc"})
    assert body["sort"] == [{"price": {"order": "desc"}}]


def test_fl_becomes_source_filtering():
    body = query_os.build_body({"fl": "id, title ,price"})
    assert body["_source"] == ["id", "title", "price"]


def test_semantic_becomes_a_knn_query():
    body = query_os.build_body(
        {"q": "quiet mouse", "semantic": True, "rows": 3,
         "vector_field": "embedding"}, embedder=StubEmbedder())
    knn = body["query"]["knn"]["embedding"]
    assert knn["k"] == 3
    assert knn["vector"][0] == 11.0        # len("quiet mouse") from the stub


def test_semantic_without_a_model_is_refused_not_silently_keyword():
    with pytest.raises(ValueError, match="model"):
        query_os.build_body({"q": "hi", "semantic": True})


def test_rows_are_validated():
    with pytest.raises(ValueError, match="Rows"):
        query_os.build_body({"rows": 5000})


# ------------------------------------------------------- error reporting ---

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_shard_failure_reason_beats_all_shards_failed():
    """The top-level reason is useless; the real explanation is nested in
    the first shard failure, sometimes several caused_by levels down."""
    msg = query_os._error_message(_Resp({"error": {
        "reason": "all shards failed",
        "failed_shards": [{"reason": {
            "reason": "Failed to parse query",
            "caused_by": {"reason": "Cannot parse 'x:[oops': Encountered EOF"},
        }}],
    }}))
    assert "Cannot parse" in msg
    assert "all shards failed" not in msg


def test_aggregating_a_text_field_explains_the_fix():
    msg = query_os._error_message(_Resp({"error": {
        "reason": "all shards failed",
        "failed_shards": [{"reason": {
            "reason": "Text fields are not optimised for operations that "
                      "require per-document field data like aggregations",
        }}],
    }}))
    assert ".keyword" in msg


# ------------------------------------------------------------- topology ---

_CAT_SHARDS = [
    {"shard": "0", "prirep": "p", "state": "STARTED",
     "node": "os1", "docs": "500", "store": "1.2mb"},
    {"shard": "0", "prirep": "r", "state": "STARTED",
     "node": "os2", "docs": "500", "store": "1.2mb"},
    {"shard": "1", "prirep": "r", "state": "UNASSIGNED",
     "node": None, "docs": None, "store": None},
    {"shard": "1", "prirep": "p", "state": "INITIALIZING",
     "node": "os2", "docs": "0", "store": "230b"},
]

_SEGMENTS = {"indices": {"products": {"shards": {
    "0": [
        {"routing": {"state": "STARTED", "primary": True, "node": "os1"},
         "segments": {
             "_0": {"num_docs": 100, "deleted_docs": 5, "size_in_bytes": 5000,
                    "committed": True, "search": True, "version": "9.7.0"},
             "_1": {"num_docs": 20, "deleted_docs": 0, "size_in_bytes": 900,
                    "committed": False, "search": True, "version": "9.7.0"},
         }},
        # the replica reports the same segments; counting it would double
        # every total on the page
        {"routing": {"state": "STARTED", "primary": False, "node": "os2"},
         "segments": {
             "_0": {"num_docs": 100, "deleted_docs": 5, "size_in_bytes": 5000,
                    "committed": True, "search": True, "version": "9.7.0"},
         }},
    ],
    "1": [
        {"routing": {"state": "STARTED", "primary": True, "node": "os2"},
         "segments": {
             "_0": {"num_docs": 80, "deleted_docs": 0, "size_in_bytes": 4000,
                    "committed": True, "search": False, "version": "9.7.0"},
         }},
    ],
}}}}


@pytest.fixture
async def mock_shards(aiohttp_server):
    async def cat_shards(request):
        return web.json_response(_CAT_SHARDS)

    async def segments(request):
        return web.json_response(_SEGMENTS)

    app = web.Application()
    app.router.add_get("/_cat/shards/products", cat_shards)
    app.router.add_get("/products/_segments", segments)
    return await aiohttp_server(app)


async def test_topology_maps_primaries_and_replicas_onto_the_solr_shape(mock_shards):
    from searchlab.topology_os import index_topology

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_topology, spec, "products")

    assert set(out["shards"]) == {"shard 0", "shard 1"}
    reps = out["shards"]["shard 0"]["replicas"]
    assert reps["primary"]["leader"] is True
    assert reps["primary"]["type"] == "primary"
    assert reps["replica 1"]["leader"] is False
    # docs come straight off _cat, so the table needs no metrics snapshot
    assert reps["primary"]["docs"] == 500


async def test_topology_translates_states_into_the_vocabulary_the_ui_colours(mock_shards):
    from searchlab.topology_os import index_topology

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_topology, spec, "products")

    shard1 = out["shards"]["shard 1"]
    # INITIALIZING is "not ready yet", which the dashboard already draws
    assert shard1["state"] == "recovering"
    assert shard1["replicas"]["primary"]["state"] == "recovering"
    # an unplaced copy has nowhere to live and no documents
    unassigned = shard1["replicas"]["replica 1"]
    assert unassigned["state"] == "down"
    assert unassigned["node"] == "unassigned"
    assert unassigned["docs"] is None


async def test_only_the_primary_offers_segments(mock_shards):
    """A replica's segments are the primary's; giving it a button would
    show one thing while labelling it another."""
    from searchlab.topology_os import index_topology

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_topology, spec, "products")

    reps = out["shards"]["shard 0"]["replicas"]
    assert reps["primary"]["core"] == "0"
    assert reps["replica 1"]["core"] == ""


async def test_topology_renders_read_only_because_os_places_copies_itself(mock_shards):
    from searchlab.topology_os import index_topology

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_topology, spec, "products")
    assert out["manage"] is False
    assert "number of replicas" in out["note"]


# ------------------------------------------------------------- segments ---

async def test_segments_count_primaries_once_not_once_per_copy(mock_shards):
    from searchlab.segments_os import index_segments

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_segments, spec, "products")

    # 2 segments on shard 0 + 1 on shard 1; the replica's copy of _0 is not
    # a third occurrence of the same 100 documents
    assert out["summary"]["count"] == 3
    assert out["summary"]["docs"] == 200
    assert out["summary"]["bytes"] == 9900


async def test_segments_report_committed_and_searchable_independently(mock_shards):
    """The state pair is the whole "I indexed it, where is it?" question:
    searchable-but-uncommitted lives in memory, committed-but-unsearchable
    is on disk waiting for a refresh."""
    from searchlab.segments_os import index_segments

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_segments, spec, "products")

    assert out["summary"]["by_source"] == {
        "committed": 1, "searchable only": 1, "committed only": 1}


async def test_segments_can_be_narrowed_to_one_shard(mock_shards):
    from searchlab.segments_os import index_segments

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_segments, spec, "products", "1")

    assert out["summary"]["count"] == 1
    assert out["core"] == "products shard 1"
    # the shard is already named in the header, so the rows do not repeat it
    assert out["segments"][0]["name"] == "_0"

    whole = await asyncio.to_thread(index_segments, spec, "products")
    # across shards the names collide (_0 exists in both), so they are tagged
    assert {s["name"] for s in whole["segments"]} == {
        "_0 (shard 0)", "_1 (shard 0)", "_0 (shard 1)"}


async def test_deleted_documents_are_reported_as_a_share_of_the_segment(mock_shards):
    from searchlab.segments_os import index_segments

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_segments, spec, "products")

    biggest = out["segments"][0]                 # _0 on shard 0, 5000 bytes
    assert biggest["deleted"] == 5
    assert biggest["deleted_pct"] == pytest.approx(4.8, abs=0.05)  # 5/105


# --------------------------------------------------------------- commit ---

async def test_commit_refreshes_and_flushes_because_os_splits_them(aiohttp_server):
    """Solr's hard commit makes documents searchable *and* durable. OS needs
    both calls to match: refreshing alone is a soft commit, which would
    leave every segment reading as searchable-but-uncommitted forever."""
    from searchlab.cluster import commit

    called = []

    async def refresh(request):
        called.append("refresh")
        return web.json_response({"_shards": {"failed": 0}})

    async def flush(request):
        called.append("flush")
        return web.json_response({"_shards": {"failed": 0}})

    app = web.Application()
    app.router.add_post("/products/_refresh", refresh)
    app.router.add_post("/products/_flush", flush)
    server = await aiohttp_server(app)

    spec = ClusterSpec(base_port=server.port, engine="opensearch")
    await asyncio.to_thread(commit, spec, "products")
    # order matters: refresh opens the searcher, flush fsyncs what is there
    assert called == ["refresh", "flush"]


# ------------------------------------------------------------ vectorize ---

@pytest.fixture
async def mock_vec(aiohttp_server):
    """An index that starts without index.knn, so the close/reopen path runs."""
    state = {"knn": False, "closed": 0, "opened": 0, "mapped": None,
             "bulks": [], "refreshed": 0, "flushed": 0, "scroll_deleted": 0,
             "props": {}}

    async def settings_get(request):
        return web.json_response({"idx": {"settings": {"index": {
            "knn": "true" if state["knn"] else "false"}}}})

    async def settings_put(request):
        body = await request.json()
        if not state["closed"]:
            return web.json_response(
                {"error": {"reason": "Can't update non dynamic settings"}}, status=400)
        state["knn"] = bool(body["index"]["knn"])
        return web.json_response({"acknowledged": True})

    async def close_idx(request):
        state["closed"] += 1
        return web.json_response({"acknowledged": True})

    async def open_idx(request):
        state["opened"] += 1
        return web.json_response({"acknowledged": True})

    async def mapping_get(request):
        return web.json_response({"idx": {"mappings": {"properties": state["props"]}}})

    async def mapping_put(request):
        state["mapped"] = await request.json()
        state["props"].update(state["mapped"]["properties"])
        return web.json_response({"acknowledged": True})

    async def search(request):
        return web.json_response({"_scroll_id": "s1", "hits": {"hits": [
            {"_id": "a", "_source": {"body": "alpha"}},
            {"_id": "b", "_source": {"body": "beta"}},
        ]}})

    async def scroll(request):
        return web.json_response({"_scroll_id": "s1", "hits": {"hits": []}})

    async def scroll_delete(request):
        state["scroll_deleted"] += 1
        return web.json_response({"succeeded": True})

    async def bulk(request):
        state["bulks"].append((await request.text()).strip().splitlines())
        return web.json_response({"errors": False, "items": []})

    async def refresh(request):
        state["refreshed"] += 1
        return web.json_response({"_shards": {"failed": 0}})

    async def flush(request):
        state["flushed"] += 1
        return web.json_response({"_shards": {"failed": 0}})

    app = web.Application()
    app.router.add_get("/idx/_settings", settings_get)
    app.router.add_put("/idx/_settings", settings_put)
    app.router.add_post("/idx/_close", close_idx)
    app.router.add_post("/idx/_open", open_idx)
    app.router.add_get("/idx/_mapping", mapping_get)
    app.router.add_put("/idx/_mapping", mapping_put)
    app.router.add_post("/idx/_search", search)
    app.router.add_post("/_search/scroll", scroll)
    app.router.add_delete("/_search/scroll", scroll_delete)
    app.router.add_post("/_bulk", bulk)
    app.router.add_post("/idx/_refresh", refresh)
    app.router.add_post("/idx/_flush", flush)
    server = await aiohttp_server(app)
    server.state = state
    return server


async def test_enabling_vectors_closes_and_reopens_because_knn_is_static(mock_vec):
    """index.knn cannot be set on an open index, so an index that was not
    built for vectors has to go down briefly. The caller is told, because
    on a real cluster that is an outage to plan for."""
    from searchlab.vectorize_os import embed_existing_docs

    spec = ClusterSpec(base_port=mock_vec.port, engine="opensearch")
    n, reopened = await asyncio.to_thread(
        embed_existing_docs, spec, "idx", StubEmbedder(), "body", "vec")

    assert reopened is True
    assert mock_vec.state["closed"] == 1 and mock_vec.state["opened"] == 1
    assert mock_vec.state["knn"] is True
    assert n == 2


async def test_an_index_already_built_for_vectors_stays_up(mock_vec):
    from searchlab.vectorize_os import embed_existing_docs

    mock_vec.state["knn"] = True
    spec = ClusterSpec(base_port=mock_vec.port, engine="opensearch")
    _, reopened = await asyncio.to_thread(
        embed_existing_docs, spec, "idx", StubEmbedder(), "body", "vec")

    assert reopened is False
    assert mock_vec.state["closed"] == 0


async def test_the_index_is_reopened_even_when_the_setting_fails(aiohttp_server):
    """Leaving an index closed would take every query down with it, so the
    reopen has to happen on the failure path too."""
    from searchlab.vectorize_os import enable_knn

    state = {"opened": 0}

    async def close_idx(request):
        return web.json_response({"acknowledged": True})

    async def settings_put(request):
        return web.json_response({"error": {"reason": "nope"}}, status=400)

    async def open_idx(request):
        state["opened"] += 1
        return web.json_response({"acknowledged": True})

    app = web.Application()
    app.router.add_post("/idx/_close", close_idx)
    app.router.add_put("/idx/_settings", settings_put)
    app.router.add_post("/idx/_open", open_idx)
    server = await aiohttp_server(app)

    spec = ClusterSpec(base_port=server.port, engine="opensearch")
    with pytest.raises(Exception):
        await asyncio.to_thread(enable_knn, spec, "idx")
    assert state["opened"] == 1


async def test_vectors_are_written_as_partial_updates_not_replacements(mock_vec):
    """A bulk `update` with a partial doc sets the vector and leaves every
    other field alone — the equivalent of Solr's atomic update. An `index`
    action would blank the rest of the document."""
    import json

    from searchlab.vectorize_os import embed_existing_docs

    spec = ClusterSpec(base_port=mock_vec.port, engine="opensearch")
    await asyncio.to_thread(
        embed_existing_docs, spec, "idx", StubEmbedder(), "body", "vec")

    lines = [json.loads(x) for x in mock_vec.state["bulks"][0]]
    assert list(lines[0]) == ["update"]
    assert lines[0]["update"]["_id"] == "a"
    assert list(lines[1]) == ["doc"]
    assert list(lines[1]["doc"]) == ["vec"]
    assert len(lines[1]["doc"]["vec"]) == StubEmbedder.dims


async def test_the_scroll_context_is_released(mock_vec):
    """A leaked scroll holds heap on every shard until it times out."""
    from searchlab.vectorize_os import embed_existing_docs

    spec = ClusterSpec(base_port=mock_vec.port, engine="opensearch")
    await asyncio.to_thread(
        embed_existing_docs, spec, "idx", StubEmbedder(), "body", "vec")
    assert mock_vec.state["scroll_deleted"] == 1


async def test_vectors_are_made_searchable_and_durable(mock_vec):
    from searchlab.vectorize_os import embed_existing_docs

    spec = ClusterSpec(base_port=mock_vec.port, engine="opensearch")
    await asyncio.to_thread(
        embed_existing_docs, spec, "idx", StubEmbedder(), "body", "vec")
    assert mock_vec.state["refreshed"] == 1 and mock_vec.state["flushed"] == 1


async def test_a_dimension_mismatch_is_explained_before_anything_is_written(mock_vec):
    """Writing the wrong width fails per-document, deep inside a bulk
    response. Catching it up front is the difference between a sentence and
    a wall of Java."""
    from searchlab.vectorize_os import ensure_vector_field

    mock_vec.state["knn"] = True
    mock_vec.state["props"] = {"vec": {"type": "knn_vector", "dimension": 384}}
    spec = ClusterSpec(base_port=mock_vec.port, engine="opensearch")

    with pytest.raises(RuntimeError, match="384-dimension"):
        await asyncio.to_thread(ensure_vector_field, spec, "idx",
                                StubEmbedder.dims, "vec")
    assert mock_vec.state["bulks"] == []


def test_a_bulk_error_surfaces_the_real_reason():
    """A bulk response is 200 even when every item failed; the reason is
    per-item, and the useful part is nested under caused_by."""
    from searchlab import vectorize_os

    body = {"errors": True, "items": [{"update": {
        "error": {"reason": "failed to parse",
                  "caused_by": {"reason": "expected 8 dimensions, got 4"}}}}]}
    assert vectorize_os._first_bulk_error(body) == "expected 8 dimensions, got 4"


# ---------------------------------------------------------------- split ---

def test_split_targets_are_only_the_multiples_the_api_accepts():
    """The API requires the source count to be a factor of the target, so
    offering anything else just produces a rejection to read."""
    from searchlab.topology_os import split_targets

    assert split_targets(2)[:4] == [4, 6, 8, 10]
    assert split_targets(3)[:3] == [6, 9, 12]
    assert all(n % 5 == 0 for n in split_targets(5))
    # splitting has to increase the count, so the current value is not offered
    assert 2 not in split_targets(2)


@pytest.fixture
async def mock_split(aiohttp_server):
    state = {"blocked": False, "split": None, "shards": 2, "fail": False}

    async def settings_get(request):
        return web.json_response({"idx": {"settings": {"index": {
            "number_of_shards": str(state["shards"])}}}})

    async def settings_put(request):
        body = await request.json()
        if body.get("index.blocks.write"):
            state["blocked"] = True
        return web.json_response({"acknowledged": True})

    async def split(request):
        if state["fail"]:
            return web.json_response(
                {"error": {"reason": "target index already exists"}}, status=400)
        state["split"] = (request.match_info["target"], await request.json())
        return web.json_response({"acknowledged": True,
                                  "shards_acknowledged": True})

    app = web.Application()
    app.router.add_get("/idx/_settings", settings_get)
    app.router.add_put("/idx/_settings", settings_put)
    app.router.add_post("/idx/_split/{target}", split)
    server = await aiohttp_server(app)
    server.state = state
    return server


async def test_split_blocks_writes_first_because_the_api_demands_it(mock_split):
    from searchlab.topology_os import split_index

    spec = ClusterSpec(base_port=mock_split.port, engine="opensearch")
    out = await asyncio.to_thread(split_index, spec, "idx", 4)

    assert mock_split.state["blocked"] is True
    target, body = mock_split.state["split"]
    assert target == "idx_s4"
    assert body["settings"]["index.number_of_shards"] == 4
    assert out["from_shards"] == 2 and out["to_shards"] == 4


async def test_a_non_multiple_is_refused_before_the_index_goes_read_only(mock_split):
    """Blocking writes and *then* failing would leave the index unable to
    take writes for no reason at all."""
    from searchlab.topology_os import split_index

    spec = ClusterSpec(base_port=mock_split.port, engine="opensearch")
    with pytest.raises(ValueError, match="multiple of 2"):
        await asyncio.to_thread(split_index, spec, "idx", 3)
    assert mock_split.state["blocked"] is False


async def test_splitting_to_the_same_or_fewer_shards_is_refused(mock_split):
    from searchlab.topology_os import split_index

    spec = ClusterSpec(base_port=mock_split.port, engine="opensearch")
    with pytest.raises(ValueError, match="already has 2 shards"):
        await asyncio.to_thread(split_index, spec, "idx", 2)
    assert mock_split.state["blocked"] is False


async def test_a_failed_split_says_the_index_is_now_read_only(mock_split):
    """By the time the split is attempted the writes are already blocked.
    Reporting only the API's reason would leave the user with an index that
    quietly stopped accepting writes."""
    from searchlab.topology_os import split_index

    mock_split.state["fail"] = True
    spec = ClusterSpec(base_port=mock_split.port, engine="opensearch")
    with pytest.raises(RuntimeError) as e:
        await asyncio.to_thread(split_index, spec, "idx", 4)

    assert "already exists" in str(e.value)          # the real reason
    assert "read-only now" in str(e.value)           # and the consequence
    assert "index.blocks.write" in str(e.value)      # and how to undo it


async def test_topology_offers_split_targets_for_its_current_shard_count(mock_shards):
    from searchlab.topology_os import index_topology

    spec = ClusterSpec(base_port=mock_shards.port, engine="opensearch")
    out = await asyncio.to_thread(index_topology, spec, "products")
    assert out["split"]["current"] == 2
    assert out["split"]["targets"][:2] == [4, 6]
    assert "read-only" in out["split"]["note"]


# ------------------------------------------------- native DSL parameters ---

def test_multi_match_type_is_carried_because_it_changes_the_query():
    """best_fields / most_fields / cross_fields are genuinely different
    queries behind one name, so the type has to reach the clause."""
    body = query_os.build_body({"q": "red shoes", "qtype": "multi_match",
                                "fields": "title^3 body",
                                "mm_type": "cross_fields", "operator": "AND"})
    mm = body["query"]["multi_match"]
    assert mm["type"] == "cross_fields"
    assert mm["fields"] == ["title^3", "body"]
    assert mm["operator"] == "AND"


def test_only_the_parameters_that_were_set_appear_in_the_body():
    """A preview full of defaults teaches nothing about what was asked for."""
    body = query_os.build_body({"q": "shoes", "qtype": "multi_match",
                                "fields": "title"})
    assert body["query"]["multi_match"] == {"query": "shoes",
                                            "fields": ["title"]}
    assert "from" not in body and "highlight" not in body
    assert "min_score" not in body and "track_total_hits" not in body


@pytest.mark.parametrize("qtype,clause", [
    ("match", "match"), ("match_phrase", "match_phrase"), ("term", "term"),
])
def test_single_field_clauses_target_the_named_field(qtype, clause):
    body = query_os.build_body({"q": "shoes", "qtype": qtype, "field": "title"})
    assert clause in body["query"]
    assert "title" in body["query"][clause]


def test_a_single_field_clause_without_a_field_says_so():
    with pytest.raises(ValueError, match="needs one field"):
        query_os.build_body({"q": "shoes", "qtype": "match"})


def test_a_bare_match_stays_bare_but_grows_when_parameters_are_added():
    """{"match": {"title": "shoes"}} is the readable form; the long form is
    only worth it once there is something to say."""
    plain = query_os.build_body({"q": "shoes", "qtype": "match",
                                 "field": "title"})
    assert plain["query"]["match"] == {"title": "shoes"}

    tuned = query_os.build_body({"q": "shoes", "qtype": "match",
                                 "field": "title", "fuzziness": "AUTO"})
    assert tuned["query"]["match"] == {"title": {"query": "shoes",
                                                 "fuzziness": "AUTO"}}


def test_fuzziness_on_a_phrase_type_is_refused_with_the_reason():
    """The engine rejects it as a shard failure; saying it up front is the
    difference between a sentence and a stack trace."""
    with pytest.raises(ValueError, match="matches terms in sequence"):
        query_os.build_body({"q": "red shoes", "qtype": "multi_match",
                             "mm_type": "phrase", "fuzziness": "AUTO"})


def test_slop_only_reaches_the_clauses_that_understand_it():
    phrase = query_os.build_body({"q": "red shoes", "qtype": "multi_match",
                                  "mm_type": "phrase", "slop": 2})
    assert phrase["query"]["multi_match"]["slop"] == 2
    # best_fields has no notion of slop, so it must not be sent one
    best = query_os.build_body({"q": "red shoes", "qtype": "multi_match",
                                "mm_type": "best_fields", "slop": 2})
    assert "slop" not in best["query"]["multi_match"]


def test_track_total_hits_is_offered_because_counts_stop_being_exact():
    body = query_os.build_body({"q": "shoes", "track_total_hits": True})
    assert body["track_total_hits"] is True


def test_highlight_accepts_either_separator():
    body = query_os.build_body({"q": "shoes", "highlight": "title, body"})
    assert body["highlight"] == {"fields": {"title": {}, "body": {}}}


def test_paging_beyond_the_result_window_is_refused():
    assert query_os.build_body({"q": "a", "from": 20})["from"] == 20
    with pytest.raises(ValueError, match="From must be between"):
        query_os.build_body({"q": "a", "from": 10_001})


@pytest.mark.parametrize("field,value,msg", [
    ("tie_breaker", 5, "Tie breaker"),
    ("operator", "MAYBE", "Operator"),
    ("fuzziness", "9", "Fuzziness"),
    ("mm_type", "nonsense", "multi_match type"),
    ("qtype", "nonsense", "Query type"),
])
def test_bad_values_are_named_rather_than_passed_through(field, value, msg):
    with pytest.raises(ValueError, match=msg):
        query_os.build_body({"q": "a", "qtype": "multi_match", field: value})


# ------------------------------------------------------------- preview ----

def test_the_preview_abbreviates_vectors_so_the_shape_stays_readable():
    """A 384-float vector *is* the preview otherwise, and the shape is the
    point — not the numbers."""
    body = query_os.preview_body(
        {"q": "quiet mouse", "semantic": True, "vector_field": "vec"},
        embedder=_LongEmbedder())
    vector = body["query"]["knn"]["vec"]["vector"]
    assert len(vector) == 4
    assert vector[-1] == "…384 floats"


def test_a_semantic_query_can_be_previewed_before_a_model_is_loaded():
    """The preview exists to show which clause the controls build; refusing
    to draw it until a 90 MB download finishes withholds exactly that."""
    body = query_os.preview_body({"q": "quiet mouse", "semantic": True})
    assert "knn" in body["query"]
    assert body["query"]["knn"]["vec"]["vector"] == [
        "<vector from the embedding model>"]


def test_running_a_query_still_needs_a_real_model():
    """Previewing with a placeholder must not make it look runnable."""
    with pytest.raises(ValueError, match="Load an embedding model"):
        query_os.build_body({"q": "quiet mouse", "semantic": True})


class _LongEmbedder:
    dims = 384

    def embed_one(self, text):
        return [0.0123456] * 384


# ------------------------------------------------ conditions (bool query) ---

def test_conditions_land_in_the_occurrence_they_were_given():
    """must and should are scored; filter and must_not are not. That is the
    distinction the condition builder exists to make visible."""
    body = query_os.build_body({"q": "shoes", "qtype": "match",
                                "field": "title", "clauses": [
        {"occur": "filter", "field": "price", "op": "range", "gte": 10},
        {"occur": "must_not", "field": "cat", "op": "is", "value": "toys"},
        {"occur": "should", "field": "title", "op": "phrase", "value": "on sale"},
    ]})
    b = body["query"]["bool"]
    assert b["filter"] == [{"range": {"price": {"gte": 10}}}]
    assert b["must_not"] == [{"term": {"cat": {"value": "toys"}}}]
    assert b["should"] == [{"match_phrase": {"title": "on sale"}}]
    assert b["must"] == [{"match": {"title": "shoes"}}]


@pytest.mark.parametrize("cond,expected", [
    ({"field": "cat", "op": "is", "value": "toys"},
     {"term": {"cat": {"value": "toys"}}}),
    ({"field": "cat", "op": "any_of", "values": "a, b"},
     {"terms": {"cat": ["a", "b"]}}),
    ({"field": "body", "op": "contains", "value": "quiet"},
     {"match": {"body": "quiet"}}),
    ({"field": "body", "op": "phrase", "value": "very quiet"},
     {"match_phrase": {"body": "very quiet"}}),
    ({"field": "cat", "op": "prefix", "value": "to"},
     {"prefix": {"cat": {"value": "to"}}}),
    ({"field": "cat", "op": "wildcard", "value": "to*s"},
     {"wildcard": {"cat": {"value": "to*s"}}}),
    ({"field": "body", "op": "exists"}, {"exists": {"field": "body"}}),
])
def test_each_operator_becomes_its_own_dsl_clause(cond, expected):
    body = query_os.build_body({"clauses": [dict(cond, occur="filter")]})
    assert body["query"]["bool"]["filter"] == [expected]


def test_numbers_stay_numbers_so_a_range_is_not_a_string_comparison():
    body = query_os.build_body({"clauses": [
        {"occur": "filter", "field": "price", "op": "range",
         "gte": "10", "lte": "99.5"}]})
    assert body["query"]["bool"]["filter"] == [
        {"range": {"price": {"gte": 10, "lte": 99.5}}}]


def test_a_bare_match_all_is_left_out_when_conditions_say_what_to_match():
    """`must: [match_all]` next to real conditions is noise in a preview
    whose job is to be read."""
    body = query_os.build_body({"q": "", "clauses": [
        {"occur": "filter", "field": "cat", "op": "is", "value": "toys"}]})
    assert "must" not in body["query"]["bool"]
    assert body["query"]["bool"]["filter"] == [{"term": {"cat": {"value": "toys"}}}]


def test_should_alone_has_to_match_something():
    """A should beside a must only boosts. On its own it would match every
    document, which is never what someone building one condition meant."""
    alone = query_os.build_body({"q": "", "clauses": [
        {"occur": "should", "field": "cat", "op": "is", "value": "toys"}]})
    assert alone["query"]["bool"]["minimum_should_match"] == 1

    beside = query_os.build_body({"q": "shoes", "qtype": "match",
                                  "field": "title", "clauses": [
        {"occur": "should", "field": "cat", "op": "is", "value": "toys"}]})
    assert "minimum_should_match" not in beside["query"]["bool"]


def test_conditions_are_ordered_the_way_the_bool_query_is_explained():
    body = query_os.build_body({"q": "shoes", "qtype": "match",
                                "field": "title", "clauses": [
        {"occur": "must_not", "field": "a", "op": "exists"},
        {"occur": "should", "field": "b", "op": "exists"},
        {"occur": "filter", "field": "c", "op": "exists"},
    ]})
    assert list(body["query"]["bool"]) == ["must", "filter", "should", "must_not"]


def test_a_condition_still_being_filled_in_sits_the_query_out():
    """The builder marks an incomplete row rather than sending a broken one,
    so typing into a fresh row does not blank the preview."""
    body = query_os.build_body({"q": "shoes", "qtype": "match",
                                "field": "title", "clauses": [
        {"occur": "filter", "field": "cat", "op": "is", "value": "", "off": True}]})
    assert "bool" not in body["query"]          # nothing left to wrap
    assert body["query"] == {"match": {"title": "shoes"}}


@pytest.mark.parametrize("cond,msg", [
    ({"occur": "filter", "op": "is", "value": "x"}, "needs a field"),
    ({"occur": "filter", "field": "a", "op": "is"}, "needs a value"),
    ({"occur": "filter", "field": "a", "op": "range"}, "needs at least one bound"),
    ({"occur": "filter", "field": "a", "op": "any_of", "values": " , "},
     "needs at least one value"),
    ({"occur": "nowhere", "field": "a", "op": "exists"}, "Condition must be one of"),
    ({"occur": "filter", "field": "a", "op": "nonsense"}, "operator must be one of"),
])
def test_an_incomplete_condition_names_what_is_missing(cond, msg):
    with pytest.raises(ValueError, match=msg):
        query_os.build_body({"clauses": [cond]})


def test_raw_fq_lines_still_work_alongside_the_menus():
    """The escape hatch stays: the menus cannot express everything Lucene can."""
    body = query_os.build_body({"fq": ["price:[1 TO 5]"], "clauses": [
        {"occur": "filter", "field": "cat", "op": "is", "value": "toys"}]})
    assert body["query"]["bool"]["filter"] == [
        {"term": {"cat": {"value": "toys"}}},
        {"query_string": {"query": "price:[1 TO 5]"}},
    ]


# --------------------------------------------------- sort and _source ------

def test_several_sort_keys_because_es_breaks_ties_with_the_next_one():
    body = query_os.build_body({"sorts": [
        {"field": "price", "order": "desc"}, {"field": "name", "order": "asc"}]})
    assert body["sort"] == [{"price": {"order": "desc"}},
                            {"name": {"order": "asc"}}]


def test_the_solr_sort_spelling_still_parses_including_several_keys():
    body = query_os.build_body({"sort": "price desc, name asc"})
    assert body["sort"] == [{"price": {"order": "desc"}},
                            {"name": {"order": "asc"}}]


def test_a_half_chosen_sort_key_is_skipped_rather_than_sent_empty():
    body = query_os.build_body({"sorts": [{"field": "", "order": "asc"},
                                          {"field": "price", "order": "asc"}]})
    assert body["sort"] == [{"price": {"order": "asc"}}]


def test_a_bad_sort_order_is_named():
    with pytest.raises(ValueError, match="asc or desc"):
        query_os.build_body({"sorts": [{"field": "price", "order": "sideways"}]})


def test_source_fields_come_from_the_picker_or_the_old_comma_string():
    assert query_os.build_body(
        {"source_fields": ["id", "title"]})["_source"] == ["id", "title"]
    assert query_os.build_body(
        {"fl": "id, title"})["_source"] == ["id", "title"]
    # nothing chosen means the whole document, so no _source key at all
    assert "_source" not in query_os.build_body({"source_fields": []})


# -------------------------------------------------------- aggregations ----

def test_a_metric_can_nest_inside_a_bucket():
    """Average price per category is the thing aggregations are for, and it
    is the one shape a terms-only facet picker cannot express."""
    body = query_os.build_body({"aggs": [
        {"type": "terms", "field": "cat", "size": 5,
         "sub_type": "avg", "sub_field": "price"}]})
    agg = body["aggs"]["terms_cat"]
    assert agg["terms"] == {"field": "cat", "size": 5}
    assert agg["aggs"] == {"avg_price": {"avg": {"field": "price"}}}


def test_nothing_can_nest_inside_a_metric():
    """A metric is one number, not a group — so there is nothing to nest in."""
    with pytest.raises(ValueError, match="nothing can nest inside it"):
        query_os.build_body({"aggs": [
            {"type": "avg", "field": "price",
             "sub_type": "max", "sub_field": "price"}]})


def test_range_bounds_are_half_open_so_bands_do_not_double_count():
    body = query_os.build_body({"aggs": [
        {"type": "range", "field": "price", "ranges": "0-100, 100-500, 500-"}]})
    assert body["aggs"]["range_price"]["range"]["ranges"] == [
        {"from": 0, "to": 100}, {"from": 100, "to": 500}, {"from": 500}]


def test_a_date_histogram_uses_calendar_intervals():
    """calendar_interval understands a month as a real month; fixed_interval
    cannot say that."""
    body = query_os.build_body({"aggs": [
        {"type": "date_histogram", "field": "created", "interval": "month"}]})
    assert body["aggs"]["date_histogram_created"]["date_histogram"] == {
        "field": "created", "calendar_interval": "month"}


@pytest.mark.parametrize("spec,msg", [
    ({"type": "terms"}, "needs a field"),
    ({"type": "nonsense", "field": "a"}, "Aggregation must be one of"),
    ({"type": "histogram", "field": "a"}, "positive interval"),
    ({"type": "range", "field": "a", "ranges": "100"}, "needs a dash"),
    ({"type": "terms", "field": "a", "sub_type": "nope", "sub_field": "b"},
     "nested aggregation must be one of"),
    ({"type": "terms", "field": "a", "sub_type": "avg"}, "nested avg needs a field"),
])
def test_a_broken_aggregation_names_what_is_wrong(spec, msg):
    with pytest.raises(ValueError, match=msg):
        query_os.build_body({"aggs": [spec]})


def test_the_older_facet_spelling_still_produces_a_terms_agg():
    body = query_os.build_body({"facet_fields": ["cat"], "facet_limit": 3})
    assert body["aggs"] == {"cat": {"terms": {"field": "cat", "size": 3}}}


def test_metric_results_are_read_as_values_not_hunted_for_buckets():
    facets, aggs = query_os._read_aggs({
        "stats_price": {"count": 3, "min": 1.0, "max": 9.0, "avg": 5.0, "sum": 15.0},
        "distinct_brand": {"value": 42},
        "p_latency": {"values": {"95.0": 210.5}},
    })
    assert facets == {}                      # nothing to click
    assert aggs["distinct_brand"] == {"kind": "value", "value": 42}
    assert aggs["p_latency"]["values"] == {"95.0": 210.5}
    assert aggs["stats_price"]["values"]["avg"] == 5.0


def test_bucket_results_carry_whatever_was_nested_in_them():
    facets, aggs = query_os._read_aggs({"by_cat": {"buckets": [
        {"key": "toys", "doc_count": 7, "avg_price": {"value": 12.5}}]}})
    # the clickable strip keeps its old shape
    assert facets["by_cat"] == [{"value": "toys", "count": 7}]
    assert aggs["by_cat"]["buckets"][0]["metrics"] == {"avg_price": 12.5}


def test_range_buckets_are_named_by_their_bounds():
    facets, _ = query_os._read_aggs({"bands": {"buckets": [
        {"from": 0.0, "to": 100.0, "doc_count": 4}]}})
    assert facets["bands"][0]["value"] == "0.0–100.0"
