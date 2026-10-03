"""Config explorer: where a value comes from, and the commands that change it."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab.config_view import (EDITABLE, USER_PROPS, _coerce, config_command,
                                   read_config)

HTML = (Path(__file__).parent.parent / "searchlab" / "templates" / "dashboard.html").read_text()

CONFIG = {"config": {
    "znodeVersion": 3,
    "updateHandler": {"autoSoftCommit": {"maxTime": 3000, "maxDocs": -1},
                      "autoCommit": {"openSearcher": False, "maxTime": 15000}},
    "query": {"filterCache": {"size": "512", "autowarmCount": "0"}},
    "indexConfig": {"ramBufferSizeMB": 100.0,
                    "mergePolicyFactory": {"segmentsPerTier": 10, "maxMergedSegmentMB": 5120.0,
                                           "deletesPctAllowed": 33.0}},
}}


def solr_app(configset: str, overlay: dict):
    async def config(request):
        return web.json_response(CONFIG)

    async def overlay_h(request):
        return web.json_response({"overlay": overlay})

    async def clusterstatus(request):
        return web.json_response({"cluster": {"collections": {
            "products": {"configName": configset}, "other": {"configName": configset}}}})

    app = web.Application()
    app.router.add_get("/solr/products/config", config)
    app.router.add_get("/solr/products/config/overlay", overlay_h)
    app.router.add_get("/solr/admin/collections", clusterstatus)
    return app


async def test_overlay_and_user_properties_are_reported(aiohttp_server):
    server = await aiohttp_server(solr_app("searchlab", {
        "props": {"updateHandler": {"autoSoftCommit": {"maxTime": 7000}, "autoCommit": {}}},
        "userProps": {"searchlab.ramBufferMB": 32}}))
    c = await asyncio.to_thread(read_config, ClusterSpec(base_port=server.port), "products")
    # empty containers left behind by unset-property are not values
    assert c["overlay"] == ["updateHandler.autoSoftCommit.maxTime"]
    assert c["lab_props"]["indexConfig.ramBufferSizeMB"] == {
        "prop": "searchlab.ramBufferMB", "file_default": 100, "set": True, "value": 32}
    assert c["lab_props"]["indexConfig.mergePolicyFactory.segmentsPerTier"]["set"] is False
    assert "znodeVersion" not in c["config"]
    assert c["shared_with"] == ["other"]


async def test_lab_properties_only_exist_in_the_lab_configset(aiohttp_server):
    server = await aiohttp_server(solr_app("_default", {}))
    c = await asyncio.to_thread(read_config, ClusterSpec(base_port=server.port), "products")
    assert c["lab_props"] == {}


def test_values_are_typed_like_the_ones_they_replace():
    assert _coerce("512", "1024") == 1024          # Solr reports cache sizes as strings
    assert _coerce(3000, "2500.0") == 2500
    assert _coerce(False, "true") is True
    assert _coerce(None, "64") == 64               # absent from the file: typed as it reads
    with pytest.raises(ValueError):
        _coerce("512", "big")
    with pytest.raises(ValueError):
        _coerce(False, "maybe")


def test_lab_settings_go_through_user_properties():
    spec = ClusterSpec()
    assert config_command(spec, "c", "indexConfig.ramBufferSizeMB", "32", reset=False) == {
        "set-user-property": {"searchlab.ramBufferMB": 32}}
    assert config_command(spec, "c", "indexConfig.ramBufferSizeMB", None, reset=True) == {
        "unset-user-property": "searchlab.ramBufferMB"}


def test_reset_removes_the_overlay_entry():
    assert config_command(ClusterSpec(), "c", "query.filterCache.size", None, reset=True) == {
        "unset-property": "query.filterCache.size"}


def test_what_solr_refuses_is_refused_before_sending():
    for path in ("indexConfig.useCompoundFile", "updateHandler.indexWriter.closeWaitsForMerges",
                 "directoryFactory.class"):
        with pytest.raises(ValueError):
            config_command(ClusterSpec(), "c", path, "x", reset=False)


# ---- the page explains every value it offers to change --------------------

HELP = set(re.findall(r'^\s*"(cfg:[^"]+)":', HTML, re.M))


def _explained(path: str) -> bool:
    cache = re.match(r"^query\.\w+Cache\.(\w+)$", path)
    return f"cfg:{path}" in HELP or bool(cache and f"cfg:query.*Cache.{cache.group(1)}" in HELP)


def test_every_changeable_value_has_an_explanation():
    missing = [p for p in sorted(EDITABLE | set(USER_PROPS)) if not _explained(p)]
    assert not missing, missing


def test_explorer_help_obeys_the_help_toggle():
    """Both explorers share one card, and it must stay shut while ? is off."""
    body = HTML[HTML.index("function fillExplorerHelp"):HTML.index("function showSchemaHelp")]
    assert "if (!paramHelpOn) return;" in body
    # and nothing looks hoverable unless help is on
    assert ".help-on [data-sh]" in HTML
    assert not re.search(r"^\[data-sh\]", HTML, re.M)
