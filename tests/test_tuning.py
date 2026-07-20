"""Tuning knobs: registry shape, config parsing, and Config API writes."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.actions import ActionRunner
from searchlab.cluster import ClusterSpec
from searchlab.configset import INDEX_CONFIG_BLOCK, patch_solrconfig
from searchlab.tuning import KNOBS, apply_tuning, read_tuning, registry, tuning_state


def test_registry_hides_solr_paths():
    reg = registry()
    assert set(reg) == set(KNOBS)
    for knob in reg.values():
        assert "path" not in knob
        assert "user_prop" not in knob
        assert "config_probe" not in knob
        assert {"label", "desc", "unit", "min", "max", "default"} <= set(knob)


def _config_payload(with_merge=False):
    config = {
        "updateHandler": {"autoSoftCommit": {"maxTime": 3000},
                          "autoCommit": {"maxTime": -1}},
        "query": {"filterCache": {"size": 512},
                  "queryResultCache": {"size": "512"}},
    }
    if with_merge:
        config["indexConfig"] = {
            "ramBufferSizeMB": 100,
            "mergePolicyFactory": {"class": "org.apache.solr.index.TieredMergePolicyFactory"},
        }
    return {"config": config}


def _make_app(posted, with_merge=False, userprops=None):
    async def get_config(request):
        return web.json_response(_config_payload(with_merge))

    async def get_overlay(request):
        return web.json_response({"overlay": {"userProps": userprops or {}}})

    async def post_config(request):
        posted.update(await request.json())
        return web.json_response({"responseHeader": {"status": 0}})

    app = web.Application()
    app.router.add_get("/solr/products/config", get_config)
    app.router.add_get("/solr/products/config/overlay", get_overlay)
    app.router.add_post("/solr/products/config", post_config)
    return app


@pytest.fixture
async def mock_config(aiohttp_server):
    posted = {}
    server = await aiohttp_server(_make_app(posted))
    server.posted = posted
    return server


@pytest.fixture
async def mock_config_merge(aiohttp_server):
    posted = {}
    server = await aiohttp_server(_make_app(
        posted, with_merge=True, userprops={"searchlab.segmentsPerTier": "4"}))
    server.posted = posted
    return server


async def test_read_tuning_converts_units_and_handles_unset(mock_config):
    spec = ClusterSpec(base_port=mock_config.port)
    values = await asyncio.to_thread(read_tuning, spec, "products")
    assert values["soft_commit_s"] == 3          # 3000 ms -> 3 s
    assert values["hard_commit_s"] is None       # -1 = disabled -> unset
    assert values["filter_cache"] == 512
    assert values["result_cache"] == 512         # overlay values arrive as strings


async def test_apply_tuning_writes_scaled_property(mock_config):
    spec = ClusterSpec(base_port=mock_config.port)
    await asyncio.to_thread(apply_tuning, spec, "products", "soft_commit_s", 10)
    assert mock_config.posted == {
        "set-property": {"updateHandler.autoSoftCommit.maxTime": 10000}}


async def test_apply_tuning_validates(mock_config):
    spec = ClusterSpec(base_port=mock_config.port)
    with pytest.raises(ValueError, match="Unknown setting"):
        await asyncio.to_thread(apply_tuning, spec, "products", "nope", 1)
    with pytest.raises(ValueError, match="between"):
        await asyncio.to_thread(apply_tuning, spec, "products", "soft_commit_s", 9999)
    assert not mock_config.posted  # nothing written on validation failure


async def test_merge_knobs_omitted_without_lab_configset(mock_config):
    spec = ClusterSpec(base_port=mock_config.port)
    state = await asyncio.to_thread(tuning_state, spec, "products")
    assert "segments_per_tier" not in state["values"]
    assert "segments_per_tier" not in state["registry"]
    assert "recreate the collection" in state["note"]


async def test_merge_knobs_present_with_lab_configset(mock_config_merge):
    spec = ClusterSpec(base_port=mock_config_merge.port)
    state = await asyncio.to_thread(tuning_state, spec, "products")
    assert "note" not in state
    assert state["values"]["segments_per_tier"] == 4     # from overlay userProps
    assert state["values"]["max_merged_mb"] is None      # unset -> UI default
    assert state["values"]["ram_buffer_mb"] is None      # user-prop unset -> UI default


async def test_apply_merge_knob_uses_set_user_property(mock_config_merge):
    spec = ClusterSpec(base_port=mock_config_merge.port)
    await asyncio.to_thread(apply_tuning, spec, "products", "segments_per_tier", 4)
    assert mock_config_merge.posted == {
        "set-user-property": {"searchlab.segmentsPerTier": 4}}


def test_patch_solrconfig_inserts_block():
    xml = "<config>\n  <indexConfig>\n    <lockType>native</lockType>\n  </indexConfig>\n</config>"
    patched = patch_solrconfig(xml)
    assert "searchlab.segmentsPerTier" in patched
    assert patched.index("<lockType>") < patched.index("mergePolicyFactory")
    assert patched.count("</indexConfig>") == 1
    # idempotent
    assert patch_solrconfig(patched) == patched


def test_patch_solrconfig_without_index_config_section():
    patched = patch_solrconfig("<config>\n  <query/>\n</config>")
    assert "<indexConfig>" in patched and "</indexConfig>" in patched
    assert "searchlab.ramBufferMB" in patched
    assert INDEX_CONFIG_BLOCK.strip().splitlines()[0].strip() in patched


async def test_action_runner_marks_config_fetch_failure_transient(aiohttp_server):
    """A collection whose /config 404s (e.g. right after CREATE, before the
    core is routable) must surface as a vague, retry-flagged error — never
    the raw httpx exception text — so the UI keeps polling instead of
    giving up on it forever."""
    from aiohttp import web

    async def not_found(request):
        return web.json_response({"error": "no such collection"}, status=404)

    app = web.Application()
    app.router.add_get("/solr/ghost/config", not_found)
    server = await aiohttp_server(app)

    runner = ActionRunner(ClusterSpec(base_port=server.port))
    out = await asyncio.to_thread(runner.read_tuning, "ghost")
    assert out["ok"] is False
    assert out.get("transient") is True
    assert "404" not in out["error"] and "Client error" not in out["error"]
