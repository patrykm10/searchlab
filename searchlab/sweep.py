"""Parameter sweeps: one workload, a matrix of cluster configs, one verdict.

"How much heap does this workload need?" and "does ZGC help here?" are the
questions a lab exists to answer. A sweep runs the identical seeded workload
against every combination in a config matrix — each cell gets a fresh cluster
(up -> collection -> index -> load -> tear down) so cells can't contaminate
each other — and produces a comparison matrix with the best cell per metric
highlighted.

    collection: products
    base:    { engine: solr, nodes: 2 }
    matrix:
      heap: ["512m", "1g", "2g"]
      gc_tune: ["", "-XX:+UseZGC"]
    workload:
      gen:  { profile: profiles/default.yaml, count: 50k, seed: 42 }
      load: { rps: 50, duration: 60, ramp: 10 }
    report: heap-sweep

Six cells here = six cluster lifecycles; sweeps are minutes-per-cell affairs
by design. The generated dataset is built once and reused across cells.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from . import cluster as cl
from . import datagen, gates, indexer, loadtest

_SWEEPABLE = ("heap", "gc_tune", "solr_opts", "solr_version", "solr_nodes", "engine")


def load_sweep(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    for section in ("collection", "matrix", "workload"):
        if section not in cfg:
            sys.exit(f"searchlab: sweep {path} needs a '{section}' section")
    if "load" not in cfg["workload"]:
        sys.exit("searchlab: sweep workload needs a 'load' section")
    for key, values in cfg["matrix"].items():
        if key not in _SWEEPABLE:
            sys.exit(f"searchlab: can't sweep '{key}' — sweepable: {', '.join(_SWEEPABLE)}")
        if not isinstance(values, list) or not values:
            sys.exit(f"searchlab: matrix '{key}' must be a non-empty list")
    return cfg


def cells(cfg: dict) -> list[dict]:
    """Cartesian product of the matrix, each cell a {param: value} dict."""
    keys = list(cfg["matrix"])
    return [dict(zip(keys, combo, strict=True))
            for combo in itertools.product(*(cfg["matrix"][k] for k in keys))]


def cell_name(cell: dict) -> str:
    return ", ".join(f"{k}={v if v != '' else 'default'}" for k, v in cell.items())


def _default_ops() -> dict:
    """Real implementations; sweeps in tests inject fakes with this shape."""
    def up(spec):
        cl.up(spec)

    def down(spec):
        cl.down(volumes=True)

    def create(spec, collection):
        cl.create_collection(spec, collection,
                             shards=spec.solr_nodes, replicas=1)

    def index(spec, collection, data_path, threads):
        return asyncio.run(indexer.index_file(
            spec.base_url(), collection, data_path,
            threads=threads, engine=spec.engine))

    def load(spec, collection, load_cfg, seed):
        return asyncio.run(loadtest.run_load(
            spec.base_url(), collection,
            rps=float(load_cfg["rps"]),
            duration=gates.parse_duration(load_cfg.get("duration", 60)),
            ramp=gates.parse_duration(load_cfg.get("ramp", 0)),
            queries_path=load_cfg.get("queries"),
            index_rps=float(load_cfg.get("index_rps", 0)),
            index_profile=load_cfg.get("index_profile"),
            seed=seed, engine=spec.engine))

    return {"up": up, "down": down, "create": create, "index": index, "load": load}


def run_sweep(cfg: dict, ops: dict | None = None, log: Callable = print) -> list[dict]:
    """Returns [{cell, name, metrics, index_docs_per_s}] in matrix order."""
    ops = ops or _default_ops()
    base = cfg.get("base", {})
    collection = cfg["collection"]
    workload = cfg["workload"]
    seed = workload.get("gen", {}).get("seed", cfg.get("seed"))

    data_path = None
    if "gen" in workload:
        g = workload["gen"]
        data_path = Path(".searchlab-sweep-data.jsonl")
        n = datagen.generate_to_file(g["profile"], gates.parse_count(g.get("count", 10_000)),
                                     data_path, seed)
        log(f"sweep: generated {n} docs once, reused for every cell")

    all_cells = cells(cfg)
    log(f"sweep: {len(all_cells)} cell(s)")
    results = []
    for i, cell in enumerate(all_cells, 1):
        name = cell_name(cell)
        spec = cl.ClusterSpec(**{**base, **cell})
        log(f"\n[{i}/{len(all_cells)}] {name}")
        t0 = time.time()
        ops["up"](spec)
        try:
            ops["create"](spec, collection)
            docs_per_s = None
            if data_path:
                stats = ops["index"](spec, collection, data_path,
                                     workload.get("index_threads", 4))
                elapsed = max(time.time() - stats.started, 1e-9)
                docs_per_s = round(stats.docs / elapsed, 0)
                log(f"  indexed {stats.docs} docs ({docs_per_s:.0f}/s)")
            result = ops["load"](spec, collection, workload["load"], seed)
            metrics = gates.result_metrics(result)
            log(f"  p50 {metrics['p50_ms']}ms  p99 {metrics['p99_ms']}ms  "
                f"errors {metrics['errors']}")
            results.append({"cell": cell, "name": name, "metrics": metrics,
                            "index_docs_per_s": docs_per_s,
                            "cell_wall_s": round(time.time() - t0, 1)})
        finally:
            ops["down"](spec)
    return results


def save_sweep(results: list[dict], cfg: dict, base: str | Path) -> tuple[Path, Path]:
    from . import report as rp

    base = Path(base)
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps({"matrix": cfg["matrix"], "results": results}, indent=2))
    html_path = base.with_suffix(".html")
    rp.html_sweep(results, html_path)
    return json_path, html_path
