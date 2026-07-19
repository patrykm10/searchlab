"""Live cluster control panel: watch and steer the lab from one page.

Serves a single self-contained page (no CDN, no build step) that polls
/api/snapshot and /api/logs, draws latency/heap/rate charts, tails cluster
logs, and exposes controls — start/stop/ramp a load test, index documents,
commit, merge — that POST back here and run on a background event loop.

Must be started from the project directory: cluster state lives in
./.searchlab (see cluster.WORKDIR), same as every other command.

`--demo` synthesizes plausible signals (heap sawtooth, GC pauses, latency
spikes, log lines) so the UI can be previewed without a running cluster;
controls are disabled in demo mode.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

import httpx

from . import metrics as m
from .actions import ActionRunner
from .cluster import WORKDIR, ClusterSpec, cluster_overview
from .insights import InsightsEngine
from .logstream import FakeLogStream, LogStream


def _live_snapshot(spec: ClusterSpec) -> dict:
    nodes = m.snapshot_cluster(spec)
    cluster: dict = {}
    try:
        cluster = cluster_overview(spec)
    except (httpx.HTTPError, OSError) as e:
        cluster = {"error": f"{type(e).__name__}"}
    return {"ts": time.time(), "spec": spec.__dict__, "nodes": nodes, "cluster": cluster,
            "loadtest": _read_live_load()}


def _read_live_load() -> dict | None:
    """Pick up rolling stats written by `searchlab load`; None if stale/absent."""
    path = WORKDIR / "live-load.json"
    try:
        data = json.loads(path.read_text())
        if time.time() - data.get("ts", 0) < 6:
            return data
    except (OSError, ValueError):
        pass
    return None


class _DemoState:
    """Synthesized signals with enough structure to look real: heap sawtooth
    per node, GC pause accumulation, cache hit-ratio drift, latency spikes."""

    def __init__(self, spec: ClusterSpec):
        self.spec = spec
        self.t0 = time.time()
        self.rng = random.Random(1)
        self.gc = [{"count": 0, "time": 0} for _ in range(spec.solr_nodes)]
        self.adds = 0
        self.commits = 0
        self.merges = [0, 0]

    def snapshot(self) -> dict:
        t = time.time() - self.t0
        nodes = {}
        for i in range(self.spec.solr_nodes):
            # Heap sawtooth: linear growth, reset (young GC) every ~25s per node phase.
            phase = (t + i * 9) % 25
            heap_used = 180 + phase * 26 + self.rng.uniform(-8, 8)
            if phase < 0.6:
                self.gc[i]["count"] += 1
                self.gc[i]["time"] += self.rng.randint(12, 60)
            spike = 260 if (t + i * 13) % 90 < 3 else 0  # periodic p99 spike
            hit = 0.62 + 0.3 * min(t / 240, 1) + self.rng.uniform(-0.02, 0.02)
            self.adds += self.rng.randint(30, 90)
            if int(t) % 15 == 0:
                self.commits += 1
            if self.rng.random() < 0.02:
                self.merges[0] += 1
            if self.rng.random() < 0.004:
                self.merges[1] += 1
            nodes[f"solr{i + 1}"] = {
                "ts": time.time(),
                "jvm": {
                    "heap_used_mb": round(heap_used, 1),
                    "heap_max_mb": 1024.0,
                    "gc": {"G1-Young-Generation": dict(self.gc[i])},
                },
                "cores": {
                    f"products_shard{i + 1}_replica_n{i + 1}": {
                        "num_docs": 500_000 + int(self.adds / self.spec.solr_nodes),
                        "deleted_docs": int(self.adds * 0.01),
                        "warmup_ms": 340,
                        "caches": {
                            "queryResultCache": {"hitratio": round(min(hit, 0.98), 3),
                                                 "size": 480, "evictions": int(t * 2)},
                            "filterCache": {"hitratio": round(min(hit + 0.07, 0.99), 3),
                                            "size": 256, "evictions": int(t)},
                            "documentCache": {"hitratio": round(max(hit - 0.25, 0.1), 3),
                                              "size": 512, "evictions": int(t * 5)},
                        },
                        "update": {
                            "adds_cumulative": self.adds,
                            "commits": self.commits,
                            "soft_commits": self.commits * 3,
                            "merges_minor": self.merges[0],
                            "merges_major": self.merges[1],
                        },
                        "select_p99_ms": round(
                            34 + 10 * math.sin(t / 17 + i) + self.rng.uniform(0, 6) + spike, 1
                        ),
                        "select_rate_1m": round(46 + 6 * math.sin(t / 31) + self.rng.uniform(-2, 2), 1),
                    }
                },
            }
        lt = None
        if 20 <= t <= 260:
            el = t - 20
            ramp = min(el / 15, 1)
            lt = {
                "ts": time.time(),
                "elapsed_s": round(el, 1),
                "duration_s": 240,
                "target_rps": 50,
                "recent_rps": round(50 * ramp + self.rng.uniform(-1.5, 1.5), 1),
                "recent_p50_ms": round(11 + 2 * math.sin(el / 9) + self.rng.uniform(0, 1.5), 1),
                "recent_p99_ms": round(30 + 8 * math.sin(el / 13) + self.rng.uniform(0, 4)
                                       + (220 if (el % 90) < 4 else 0), 1),
                "errors": int(el // 85),
                "dropped": 0,
                "requests": int(50 * max(el - 7.5, 0)),
            }
        return {
            "ts": time.time(),
            "spec": self.spec.__dict__,
            "nodes": nodes,
            "cluster": {"live_nodes": self.spec.solr_nodes,
                        "collections": {"products": {"shards": self.spec.solr_nodes, "health": "GREEN"}}},
            "loadtest": lt,
        }


def _load_page() -> bytes:
    return (resources.files("searchlab") / "templates" / "dashboard.html").read_bytes()


def make_handler(spec: ClusterSpec, demo: bool,
                 runner: ActionRunner | None = None, logs: LogStream | None = None):
    demo_state = _DemoState(spec) if demo else None
    page = _load_page()
    lock = threading.Lock()
    insights = InsightsEngine()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/snapshot":
                with lock:
                    snap = demo_state.snapshot() if demo_state else _live_snapshot(spec)
                    snap["insights"] = insights.analyze(snap)
                snap["actions"] = runner.state() if runner else {"demo": True}
                self._json(200, snap)
            elif path == "/api/logs":
                if logs is None:
                    return self._json(200, {"latest": 0, "lines": [], "error": None})
                qs = parse_qs(urlparse(self.path).query)
                try:
                    since = int(qs.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                latest, lines = logs.since(since)
                self._json(200, {"latest": latest, "lines": lines, "error": logs.error})
            else:
                self._send(404, b"not found", "text/plain")

        def _route_post(self, path: str, body: dict) -> dict:
            if runner is None:
                return {"ok": False, "error": "Controls are disabled in demo mode."}
            coll = str(body.get("collection", "")).strip()
            if path == "/api/load/start":
                return runner.start_load(coll, float(body.get("rps", 0)))
            if path == "/api/load/rps":
                return runner.set_rps(float(body.get("rps", 0)))
            if path == "/api/load/stop":
                return runner.stop_load()
            if path == "/api/index":
                return runner.index_docs(coll, int(body.get("count", 0)))
            if path == "/api/commit":
                return runner.commit(coll)
            if path == "/api/optimize":
                return runner.optimize(coll)
            return {"ok": False, "error": f"unknown action: {path}"}

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "error": "invalid JSON body"})
            try:
                out = self._route_post(urlparse(self.path).path, body)
            except (SystemExit, Exception) as e:  # noqa: BLE001 — surface, don't die
                return self._json(500, {"ok": False, "error": str(e) or type(e).__name__})
            self._json(200 if out.get("ok") else 409, out)

    return Handler


def serve(spec: ClusterSpec, port: int = 8990, demo: bool = False) -> None:
    runner = None if demo else ActionRunner(spec)
    logs = FakeLogStream() if demo else LogStream(WORKDIR / "docker-compose.yml")
    logs.start()
    server = ThreadingHTTPServer(("127.0.0.1", port),
                                 make_handler(spec, demo, runner, logs))
    mode = "demo signals, controls disabled" if demo else f"live cluster ({spec.solr_nodes} node(s))"
    print(f"searchlab control panel on http://localhost:{port}  [{mode}]  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        logs.stop()
