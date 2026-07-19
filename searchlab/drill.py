"""Orchestrated failure drills: one YAML, one command, one report.

A drill combines everything the tool knows into a single reproducible run:
metrics snapshot -> open-loop load with timed chaos injected mid-flight ->
metrics snapshot -> a self-contained HTML report where the fault injections
are drawn as annotations on the latency timeline, next to the before/after
metrics diff and the latency histogram.

    collection: products
    load:    { rps: 50, duration: 240, ramp: 15, index_rps: 10 }
    chaos:
      - { at: 60,  action: pause,   node: solr2 }
      - { at: 120, action: unpause, node: solr2 }
    seed: 42
    report: drill

Deterministic load + deterministic faults = run the same drill on two engine
versions and diff what happened.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from . import chaos as ch
from . import loadtest
from . import metrics as m
from .cluster import WORKDIR, ClusterSpec


def load_drill(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    if not isinstance(cfg, dict) or "collection" not in cfg or "load" not in cfg:
        sys.exit(f"searchlab: drill {path} needs at least 'collection' and 'load' sections")
    load = cfg["load"]
    if "rps" not in load or "duration" not in load:
        sys.exit("searchlab: drill load section needs 'rps' and 'duration'")
    for s in cfg.get("chaos", []):
        if s.get("action") not in ch._ACTIONS or "at" not in s or "node" not in s:
            sys.exit(f"searchlab: bad chaos step {s} — needs at/action/node, "
                     f"action one of {', '.join(ch._ACTIONS)}")
        if float(s["at"]) >= float(load["duration"]):
            sys.exit(f"searchlab: chaos step at t={s['at']} is outside the "
                     f"{load['duration']}s load window")
    return cfg


async def _run_chaos(spec: ClusterSpec, steps: list[dict],
                     inject: Callable, log: Callable) -> list[dict]:
    """Execute timed steps without blocking the load's event loop; docker
    calls run in a worker thread. Returns executed events with actual times."""
    events = []
    t0 = time.perf_counter()
    for s in sorted(steps, key=lambda x: float(x["at"])):
        wait = float(s["at"]) - (time.perf_counter() - t0)
        if wait > 0:
            await asyncio.sleep(wait)
        actual = time.perf_counter() - t0
        await asyncio.to_thread(inject, spec, s["action"], str(s["node"]))
        log(f"[t={actual:6.1f}s] chaos: {s['action']} {s['node']}")
        events.append({"at_s": round(actual, 1), "action": s["action"],
                       "node": str(s["node"])})
    return events


def _default_inject(spec: ClusterSpec, action: str, node: str) -> None:
    ch._ACTIONS[action](spec, node)


async def run_drill(
    spec: ClusterSpec,
    cfg: dict,
    inject: Callable = _default_inject,
    snapshot: Callable = m.snapshot_cluster,
    log: Callable = print,
) -> dict:
    """Returns {report (load JSON dict), events, metrics_before, metrics_after}."""
    load_cfg = cfg["load"]
    collection = cfg["collection"]

    log("drill: metrics snapshot (before)")
    before = snapshot(spec)

    log(f"drill: load {load_cfg['rps']} rps for {load_cfg['duration']}s "
        f"+ {len(cfg.get('chaos', []))} chaos step(s)")
    load_task = asyncio.create_task(loadtest.run_load(
        spec.base_url(), collection,
        rps=float(load_cfg["rps"]),
        duration=float(load_cfg["duration"]),
        ramp=float(load_cfg.get("ramp", 0)),
        queries_path=load_cfg.get("queries"),
        index_rps=float(load_cfg.get("index_rps", 0)),
        index_profile=load_cfg.get("index_profile"),
        seed=cfg.get("seed"),
        live_file=WORKDIR / "live-load.json" if WORKDIR.exists() else None,
        engine=spec.engine,
    ))
    chaos_task = asyncio.create_task(
        _run_chaos(spec, cfg.get("chaos", []), inject, log))
    result, events = await asyncio.gather(load_task, chaos_task)

    log("drill: metrics snapshot (after)")
    after = snapshot(spec)

    return {"result": result, "events": events,
            "metrics_before": before, "metrics_after": after}


def save_drill(outcome: dict, base: str | Path, title: str = "searchlab drill") -> tuple[Path, Path]:
    """Write <base>.json and <base>.html; returns both paths."""
    from . import report as rp

    base = Path(base)
    result = outcome["result"]
    json_path = base.with_suffix(".json")
    loadtest.save_report(result, json_path)
    data = json.loads(json_path.read_text())
    data["events"] = outcome["events"]
    data["metrics_diff"] = m.diff_snapshots(
        outcome["metrics_before"], outcome["metrics_after"])
    json_path.write_text(json.dumps(data, indent=2))

    html_path = base.with_suffix(".html")
    rp.html_drill(json_path, html_path, title=title)
    return json_path, html_path
