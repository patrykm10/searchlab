"""Fault injection for SolrCloud learning and incident repro.

Killing or pausing a node mid-load-test is how you actually learn (and
demonstrate) leader election, replica recovery, and what queries do while a
shard has no leader. `kill` sends SIGKILL (hard crash, no graceful shutdown);
`pause` freezes the JVM with SIGSTOP, which looks like a long GC pause or a
hung node to the rest of the cluster — a much nastier and more realistic
failure mode than a clean death.
"""

from __future__ import annotations

import subprocess
import sys

from .cluster import ClusterSpec


def _container(spec: ClusterSpec, node: str) -> str:
    """Accept 'solr2', '2', or 'zk1' and return the container name."""
    if node.isdigit():
        node = f"{spec.eng().node_prefix}{node}"
    valid = spec.eng().node_names(spec)
    if node not in valid:
        sys.exit(f"solrlab: unknown node '{node}' — valid: {', '.join(valid)}")
    return f"{spec.project_name}-{node}"


def _docker(*args: str) -> None:
    r = subprocess.run(["docker", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"solrlab: docker {' '.join(args)} failed: {r.stderr.strip()}")


def kill(spec: ClusterSpec, node: str) -> str:
    """SIGKILL the node: hard crash, no graceful shutdown, no goodbye to ZK."""
    c = _container(spec, node)
    _docker("kill", c)
    return c


def pause(spec: ClusterSpec, node: str) -> str:
    """SIGSTOP the node: process frozen but alive. Mimics a stop-the-world GC
    pause or hung JVM — ZK session eventually expires, cluster reacts."""
    c = _container(spec, node)
    _docker("pause", c)
    return c


def unpause(spec: ClusterSpec, node: str) -> str:
    c = _container(spec, node)
    _docker("unpause", c)
    return c


def start(spec: ClusterSpec, node: str) -> str:
    """Bring a killed node back; watch it rejoin and recover replicas."""
    c = _container(spec, node)
    _docker("start", c)
    return c


def restart(spec: ClusterSpec, node: str) -> str:
    """Graceful stop + start (orderly shutdown, unlike kill)."""
    c = _container(spec, node)
    _docker("restart", c)
    return c


# -------------------------------------------------------------- scenarios ---

_ACTIONS = {"kill": kill, "pause": pause, "unpause": unpause, "start": start, "restart": restart}


def load_scenario(path) -> list[dict]:
    from pathlib import Path

    import yaml

    data = yaml.safe_load(Path(path).read_text())
    steps = data.get("steps", data) if isinstance(data, dict) else data
    if not isinstance(steps, list) or not steps:
        sys.exit(f"solrlab: no steps found in scenario {path}")
    for s in steps:
        if s.get("action") not in _ACTIONS:
            sys.exit(f"solrlab: unknown action '{s.get('action')}' — valid: {', '.join(_ACTIONS)}")
        if "at" not in s or "node" not in s:
            sys.exit(f"solrlab: scenario steps need 'at' (seconds), 'action', 'node': {s}")
    return sorted(steps, key=lambda s: float(s["at"]))


def run_scenario(spec: ClusterSpec, path, log=print) -> None:
    """Execute timed fault steps against the wall clock. Run this alongside a
    load test; the report timeline plus this log is a reproducible drill."""
    import time

    steps = load_scenario(path)
    t0 = time.time()
    log(f"scenario {path}: {len(steps)} step(s), total {steps[-1]['at']}s")
    for s in steps:
        wait = float(s["at"]) - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)
        c = _ACTIONS[s["action"]](spec, str(s["node"]))
        log(f"[t={time.time() - t0:6.1f}s] {s['action']} {c}")
    log("scenario complete")
