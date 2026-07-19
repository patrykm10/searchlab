"""Cluster lifecycle: render docker-compose, bring SolrCloud up/down, create collections."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from jinja2 import Environment, PackageLoader

from .engines import get_engine

WORKDIR = Path.cwd() / ".solrlab"

PROMETHEUS_CONFIG = """\
global:
  scrape_interval: 10s
scrape_configs:
  - job_name: solr
    static_configs:
      - targets: ["solr-exporter:9854"]
"""


@dataclass
class ClusterSpec:
    engine: str = "solr"
    solr_version: str = "9.6"
    solr_nodes: int = 2
    zk_nodes: int = 1
    zk_version: str = "3.9"
    heap: str = "1g"
    gc_tune: str = ""
    solr_opts: str = ""
    base_port: int = 8983
    monitoring: bool = False
    gc_logs: bool = False
    project_name: str = "solrlab"

    def base_url(self, node: int = 0) -> str:
        return self.eng().base_url(self, node)

    def eng(self):
        return get_engine(self.engine)


def _compose_cmd() -> list[str]:
    """Prefer `docker compose` (v2), fall back to `docker-compose`."""
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    sys.exit("solrlab: docker (with compose plugin) or docker-compose not found on PATH")


def render_compose(spec: ClusterSpec) -> str:
    env = Environment(loader=PackageLoader("solrlab", "templates"), keep_trailing_newline=True)
    tpl = env.get_template(get_engine(spec.engine).compose_template)
    return tpl.render(**spec.__dict__)


def write_workdir(spec: ClusterSpec) -> Path:
    WORKDIR.mkdir(exist_ok=True)
    compose_path = WORKDIR / "docker-compose.yml"
    compose_path.write_text(render_compose(spec))
    (WORKDIR / "spec.json").write_text(json.dumps(spec.__dict__, indent=2))
    if spec.monitoring:
        (WORKDIR / "prometheus.yml").write_text(PROMETHEUS_CONFIG)
    if spec.gc_logs:
        for i in range(spec.solr_nodes):
            (WORKDIR / "gc-logs" / f"solr{i + 1}").mkdir(parents=True, exist_ok=True)
    return compose_path


def load_spec() -> ClusterSpec:
    spec_path = WORKDIR / "spec.json"
    if not spec_path.exists():
        sys.exit("solrlab: no cluster found in ./.solrlab — run `solrlab up` first")
    return ClusterSpec(**json.loads(spec_path.read_text()))


def up(spec: ClusterSpec, wait: bool = True, timeout: int = 180) -> None:
    compose_path = write_workdir(spec)
    cmd = _compose_cmd() + ["-f", str(compose_path), "up", "-d"]
    subprocess.run(cmd, check=True)
    if wait:
        wait_healthy(spec, timeout=timeout)


def down(volumes: bool = False) -> None:
    compose_path = WORKDIR / "docker-compose.yml"
    if not compose_path.exists():
        sys.exit("solrlab: nothing to tear down (no ./.solrlab/docker-compose.yml)")
    cmd = _compose_cmd() + ["-f", str(compose_path), "down"]
    if volumes:
        cmd.append("-v")
    subprocess.run(cmd, check=True)


def wait_healthy(spec: ClusterSpec, timeout: int = 180) -> None:
    """Block until every Solr node answers its admin ping."""
    deadline = time.time() + timeout
    pending = {i for i in range(spec.solr_nodes)}
    eng = spec.eng()
    print(f"Waiting for {spec.solr_nodes} {spec.engine} node(s) to become healthy...")
    with httpx.Client(timeout=5) as client:
        while pending and time.time() < deadline:
            for i in sorted(pending):
                try:
                    if eng.health_ok(client, spec, i):
                        pending.discard(i)
                        print(f"  {eng.node_prefix}{i + 1} (port {spec.base_port + i}) is up")
                except httpx.HTTPError:
                    pass
            if pending:
                time.sleep(2)
    if pending:
        sys.exit(f"solrlab: nodes not healthy after {timeout}s: {sorted(pending)}")



def create_collection(spec: ClusterSpec, name: str, shards: int = 1,
                      replicas: int = 1, config_set: str = "_default") -> None:
    spec.eng().create_index(spec, name, shards, replicas, config_set)


def delete_collection(spec: ClusterSpec, name: str) -> None:
    spec.eng().delete_index(spec, name)


def cluster_overview(spec: ClusterSpec) -> dict:
    """Engine-normalized: {live_nodes, collections: {name: {shards, health}}}."""
    return spec.eng().cluster_overview(spec)
