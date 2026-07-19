"""Assertion gates, human units, and CLI integration."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from searchlab.gates import (
    check_assertions,
    parse_assertion,
    parse_count,
    parse_duration,
    result_metrics,
)
from searchlab.loadtest import run_load


def test_parse_assertion_forms():
    assert parse_assertion("p99_ms<50") == ("p99_ms", "<", 50.0)
    assert parse_assertion(" errors = 0 ") == ("errors", "=", 0.0)
    assert parse_assertion("achieved_rps>=45.5") == ("achieved_rps", ">=", 45.5)
    for bad in ("p99_ms<", "nope<5", "p99_ms~5", "p99<50ms"):
        with pytest.raises(SystemExit):
            parse_assertion(bad)


def test_check_assertions():
    report = {"p99_ms": 42.0, "errors": 0, "achieved_rps": 48.7}
    assert check_assertions(report, ["p99_ms<50", "errors=0"]) == []
    fails = check_assertions(report, ["p99_ms<40", "errors!=0", "p50_ms<10"])
    assert len(fails) == 3
    assert "actual p99_ms = 42.0" in fails[0]
    assert "missing" in fails[2]


def test_parse_duration():
    assert parse_duration("90") == 90
    assert parse_duration("90s") == 90
    assert parse_duration("2m") == 120
    assert parse_duration("2m30s") == 150
    assert parse_duration("1h30m") == 5400
    assert parse_duration(45.5) == 45.5
    with pytest.raises(SystemExit):
        parse_duration("soon")


def test_parse_count():
    assert parse_count("10000") == 10_000
    assert parse_count("10k") == 10_000
    assert parse_count("1.5m") == 1_500_000
    assert parse_count("1_000") == 1_000
    assert parse_count(7) == 7
    with pytest.raises(SystemExit):
        parse_count("many")


@pytest.fixture
async def mock_solr(aiohttp_server):
    async def select(request):
        await asyncio.sleep(0.005)
        return web.json_response({"response": {}})
    app = web.Application()
    app.router.add_get("/solr/t/select", select)
    return await aiohttp_server(app)


async def test_result_metrics_match_saved_report(mock_solr, tmp_path):
    from searchlab.loadtest import save_report
    base = f"http://{mock_solr.host}:{mock_solr.port}/solr"
    result = await run_load(base, "t", rps=40, duration=2, seed=1)
    metrics = result_metrics(result)
    out = tmp_path / "r.json"
    save_report(result, out)
    saved = json.loads(out.read_text())
    for k, v in metrics.items():
        assert saved[k] == v, k
    # a realistic gate against the mock passes
    assert check_assertions(metrics, ["errors=0", "p99_ms<500", "achieved_rps>30"]) == []
