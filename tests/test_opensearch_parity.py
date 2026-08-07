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
    assert all("shard 1" in s["name"] for s in out["segments"])


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
