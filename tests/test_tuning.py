"""Tuning knobs: registry shape, config parsing, and Config API writes."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab.tuning import KNOBS, apply_tuning, read_tuning, registry


def test_registry_hides_solr_paths():
    reg = registry()
    assert set(reg) == set(KNOBS)
    for knob in reg.values():
        assert "path" not in knob
        assert {"label", "desc", "unit", "min", "max", "default"} <= set(knob)


@pytest.fixture
async def mock_config(aiohttp_server):
    posted = {}

    async def get_config(request):
        return web.json_response({"config": {
            "updateHandler": {"autoSoftCommit": {"maxTime": 3000},
                              "autoCommit": {"maxTime": -1}},
            "query": {"filterCache": {"size": 512},
                      "queryResultCache": {"size": "512"}},
        }})

    async def post_config(request):
        posted.update(await request.json())
        return web.json_response({"responseHeader": {"status": 0}})

    app = web.Application()
    app.router.add_get("/solr/products/config", get_config)
    app.router.add_post("/solr/products/config", post_config)
    server = await aiohttp_server(app)
    server.posted = posted
    return server


async def test_read_tuning_converts_units_and_handles_unset(mock_config):
    spec = ClusterSpec(base_port=mock_config.port)
    values = await asyncio.to_thread(read_tuning, spec, "products")
    assert values["soft_commit_s"] == 3          # 3000 ms -> 3 s
    assert values["hard_commit_s"] is None       # -1 = disabled -> unset
    assert values["filter_cache"] == 512
    assert values["result_cache"] is None        # string in config -> not numeric


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
