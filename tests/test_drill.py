"""Drill orchestration tests: validation, timed execution, combined report."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from searchlab.cluster import ClusterSpec
from searchlab.drill import load_drill, run_drill, save_drill


def _write(tmp_path, text):
    p = tmp_path / "drill.yaml"
    p.write_text(text)
    return p


def test_drill_validation(tmp_path):
    ok = _write(tmp_path, """
collection: c
load: {rps: 10, duration: 30}
chaos:
  - {at: 5, action: pause, node: solr2}
""")
    cfg = load_drill(ok)
    assert cfg["load"]["rps"] == 10

    with pytest.raises(SystemExit):  # missing load
        load_drill(_write(tmp_path, "collection: c\n"))
    with pytest.raises(SystemExit):  # bad action
        load_drill(_write(tmp_path,
            "collection: c\nload: {rps: 1, duration: 10}\n"
            "chaos: [{at: 2, action: explode, node: solr1}]\n"))
    with pytest.raises(SystemExit):  # chaos outside load window
        load_drill(_write(tmp_path,
            "collection: c\nload: {rps: 1, duration: 10}\n"
            "chaos: [{at: 15, action: kill, node: solr1}]\n"))


@pytest.fixture
async def mock_solr(aiohttp_server):
    async def select(request):
        await asyncio.sleep(0.003)
        return web.json_response({"response": {}})
    app = web.Application()
    app.router.add_get("/solr/c/select", select)
    return await aiohttp_server(app)


async def test_run_drill_end_to_end(mock_solr, tmp_path, monkeypatch):
    spec = ClusterSpec(base_port=mock_solr.port)
    # point spec URLs at the mock (host may differ from localhost)
    monkeypatch.setattr(ClusterSpec, "base_url",
                        lambda self, node=0: f"http://{mock_solr.host}:{mock_solr.port}/solr")

    injected = []
    snapshots = []

    def fake_inject(spec_, action, node):
        injected.append((action, node))

    def fake_snapshot(spec_):
        snapshots.append(1)
        return {"solr1": {"ts": len(snapshots) * 100.0,
                          "jvm": {"heap_used_mb": 1, "heap_max_mb": 2,
                                  "gc": {"G1": {"count": len(snapshots), "time": len(snapshots) * 10}}},
                          "cores": {}}}

    cfg = {
        "collection": "c", "seed": 1,
        "load": {"rps": 30, "duration": 3},
        "chaos": [{"at": 0.5, "action": "pause", "node": "solr2"},
                  {"at": 1.5, "action": "unpause", "node": "solr2"}],
    }
    outcome = await run_drill(spec, cfg, inject=fake_inject,
                              snapshot=fake_snapshot, log=lambda *_: None)

    assert injected == [("pause", "solr2"), ("unpause", "solr2")]
    assert len(snapshots) == 2  # before + after
    events = outcome["events"]
    assert events[0]["at_s"] == pytest.approx(0.5, abs=0.2)
    assert events[1]["at_s"] == pytest.approx(1.5, abs=0.2)
    assert len(outcome["result"].records) > 50  # load actually ran throughout

    json_path, html_path = save_drill(outcome, tmp_path / "out")
    data = json.loads(json_path.read_text())
    assert data["events"] == events
    assert "+1 pauses" in data["metrics_diff"]  # gc count 1 -> 2

    html = html_path.read_text()
    assert "pause solr2" in html            # annotation label
    assert html.count("stroke-dasharray=\"5 4\"") == 2  # two fault markers
    assert "Metrics before" in html and "+1 pauses" in html
    assert "Latency distribution" in html
