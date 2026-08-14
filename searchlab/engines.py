"""Engine abstraction: Solr, OpenSearch, and Elasticsearch behind one CLI.

Each engine knows its compose template, URLs, health checks, index creation,
bulk-indexing wire format, search request shape, and how to normalize its
stats API into the common snapshot shape the dashboard and metrics tools use.

Solr remains the first-class citizen (replay and gclog are Solr-specific for
now); ES/OS get the full cluster/gen/index/load/chaos/dashboard loop.
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque

import httpx


class Engine:
    name = "base"
    node_prefix = "node"
    default_port = 9200
    compose_template = ""

    def node_names(self, spec) -> list[str]:
        return [f"{self.node_prefix}{i + 1}" for i in range(spec.solr_nodes)]

    def base_url(self, spec, node: int = 0) -> str:
        raise NotImplementedError

    def health_ok(self, client: httpx.Client, spec, node: int) -> bool:
        raise NotImplementedError

    def create_index(self, spec, name, shards, replicas, config_set="_default") -> None:
        raise NotImplementedError

    def delete_index(self, spec, name) -> None:
        raise NotImplementedError

    def cluster_overview(self, spec) -> dict:
        """{live_nodes: int, collections: {name: {shards, health}}}"""
        raise NotImplementedError

    def bulk_request(self, base_url, collection, docs, commit_within) -> dict:
        """kwargs for httpx.AsyncClient.request covering one indexing batch."""
        raise NotImplementedError

    def search_request(self, base_url, collection, template) -> dict:
        """kwargs for httpx request from a (substituted) query template."""
        raise NotImplementedError

    def default_queries(self) -> list[dict]:
        raise NotImplementedError

    def snapshot_node(self, base_url) -> dict:
        """Normalize node stats to the common snapshot shape."""
        raise NotImplementedError


# ------------------------------------------------------------------- solr ---

class SolrEngine(Engine):
    name = "solr"
    node_prefix = "solr"
    default_port = 8983
    compose_template = "docker-compose.yml.j2"

    def node_names(self, spec):
        return super().node_names(spec) + [f"zk{i + 1}" for i in range(spec.zk_nodes)]

    def base_url(self, spec, node: int = 0) -> str:
        return f"http://localhost:{spec.base_port + node}/solr"

    def health_ok(self, client, spec, node):
        r = client.get(f"{self.base_url(spec, node)}/admin/info/system", params={"wt": "json"})
        return r.status_code == 200

    def create_index(self, spec, name, shards, replicas, config_set="_default"):
        r = httpx.get(
            f"{self.base_url(spec)}/admin/collections",
            params={"action": "CREATE", "name": name, "numShards": shards,
                    "replicationFactor": replicas, "collection.configName": config_set,
                    "wt": "json"},
            timeout=60,
        )
        body = r.json()
        if r.status_code != 200 or body.get("failure"):
            sys.exit(f"searchlab: collection create failed: {json.dumps(body, indent=2)}")

    def delete_index(self, spec, name):
        httpx.get(f"{self.base_url(spec)}/admin/collections",
                  params={"action": "DELETE", "name": name, "wt": "json"}, timeout=60)

    def cluster_overview(self, spec) -> dict:
        r = httpx.get(f"{self.base_url(spec)}/admin/collections",
                      params={"action": "CLUSTERSTATUS", "wt": "json"}, timeout=10)
        r.raise_for_status()
        state = r.json().get("cluster", {})
        return {
            "live_nodes": len(state.get("live_nodes", [])),
            "collections": {n: {"shards": len(c.get("shards", {})),
                                "health": c.get("health", "?")}
                            for n, c in state.get("collections", {}).items()},
        }

    def bulk_request(self, base_url, collection, docs, commit_within):
        return {"method": "POST", "url": f"{base_url}/{collection}/update",
                "params": {"commitWithin": commit_within, "wt": "json"}, "json": docs}

    def search_request(self, base_url, collection, template):
        params = dict(template.get("params", {}))
        params.setdefault("wt", "json")
        return {"method": "GET", "url": f"{base_url}/{collection}/select", "params": params}

    def default_queries(self):
        return [{"name": "match_all", "weight": 1, "params": {"q": "*:*", "rows": 10}}]

    def snapshot_node(self, base_url) -> dict:
        from . import metrics
        return metrics.snapshot_node_solr(base_url)


# --------------------------------------------------------------- es-family ---

# Solr publishes a ready-made 1-minute rate meter; ES/OS publish only the
# cumulative search counters, so a rate has to be derived here. Measuring it
# against just the previous sample divides by whatever gap that caller
# happened to leave: two pollers landing together — a second dashboard tab,
# or the CLI alongside the browser — give a sub-millisecond gap, and a
# one-query delta over it reads as hundreds of requests a second. Averaging
# over a fixed window keeps the answer steady however often it is asked.
_SEARCH_SAMPLES: dict[str, deque[tuple[float, int, int]]] = {}
_RATE_WINDOW_S = 60.0   # matches the "1m" the Solr side reports


class _EsFamily(Engine):
    """Shared behavior for Elasticsearch and OpenSearch (same wire APIs)."""

    compose_template = "docker-compose-es.yml.j2"

    def base_url(self, spec, node: int = 0) -> str:
        return f"http://localhost:{spec.base_port + node}"

    def health_ok(self, client, spec, node):
        return client.get(self.base_url(spec, node)).status_code == 200

    def create_index(self, spec, name, shards, replicas, config_set="_default"):
        settings = {"number_of_shards": shards,
                    "number_of_replicas": max(replicas - 1, 0),
                    # Solr logs every request; these engines log none unless
                    # a slow-query threshold is crossed. Zero means "report
                    # all of them", which is what gives the traffic panel
                    # something to show. Reasonable for a lab and wrong for
                    # production, where this writes a line per query.
                    "index.search.slowlog.threshold.query.trace": "0ms"}
        if self.name == "opensearch":
            # index.knn is static (create-time only); always on for lab indexes
            settings["index.knn"] = True
        r = httpx.put(
            f"{self.base_url(spec)}/{name}",
            json={"settings": settings},
            timeout=60,
        )
        if r.status_code != 200:
            sys.exit(f"searchlab: index create failed: {r.text[:400]}")

    def delete_index(self, spec, name):
        httpx.delete(f"{self.base_url(spec)}/{name}", timeout=60)

    def cluster_overview(self, spec) -> dict:
        base = self.base_url(spec)
        health = httpx.get(f"{base}/_cluster/health", timeout=10).json()
        indices = httpx.get(f"{base}/_cat/indices", params={"format": "json"}, timeout=10).json()
        return {
            "live_nodes": health.get("number_of_nodes", 0),
            "collections": {i["index"]: {"shards": int(i.get("pri", 0)),
                                         "health": i.get("health", "?").upper()}
                            for i in indices if not i["index"].startswith(".")},
        }

    def bulk_request(self, base_url, collection, docs, commit_within):
        lines = []
        for d in docs:
            action = {"index": {}}
            if "id" in d:
                action["index"]["_id"] = d["id"]
            lines.append(json.dumps(action))
            lines.append(json.dumps(d))
        return {"method": "POST", "url": f"{base_url}/{collection}/_bulk",
                "content": "\n".join(lines) + "\n",
                "headers": {"Content-Type": "application/x-ndjson"}}

    def search_request(self, base_url, collection, template):
        return {"method": "POST", "url": f"{base_url}/{collection}/_search",
                "json": template.get("body", {"query": {"match_all": {}}, "size": 10})}

    def default_queries(self):
        return [{"name": "match_all", "weight": 1,
                 "body": {"query": {"match_all": {}}, "size": 10}}]

    def snapshot_node(self, base_url) -> dict:
        with httpx.Client(timeout=15) as client:
            stats = client.get(
                f"{base_url}/_nodes/_local/stats/jvm,indices,thread_pool,breaker,os,process"
            ).json()
        node = next(iter(stats["nodes"].values()))
        jvm, idx = node["jvm"], node["indices"]

        # The search thread pool is where query rejection actually happens,
        # and unlike Solr's Jetty pool this reports `rejected` directly — so
        # "did the engine turn work away?" has a real answer rather than an
        # inference. Utilization is derived to match the Solr-side shape.
        search_pool = (node.get("thread_pool") or {}).get("search") or {}
        threads = search_pool.get("threads") or 0
        active = search_pool.get("active") or 0
        pool = {
            "size": threads,
            "active": active,
            "queued": search_pool.get("queue") or 0,
            "rejected": search_pool.get("rejected") or 0,
            "utilization": round(active / threads, 4) if threads else None,
        }

        # OpenSearch/Elasticsearch ship circuit breakers enabled by default,
        # so a trip count here is meaningful (on Solr they are usually not
        # configured at all).
        breakers = {name: b.get("tripped", 0)
                    for name, b in (node.get("breakers") or {}).items()}
        gc = {name: {"count": c.get("collection_count", 0),
                     "time": c.get("collection_time_in_millis", 0)}
              for name, c in jvm.get("gc", {}).get("collectors", {}).items()}
        qc = idx.get("query_cache", {})
        rc = idx.get("request_cache", {})

        def ratio(d):
            h, m = d.get("hit_count", 0), d.get("miss_count", 0)
            return round(h / (h + m), 3) if h + m else None

        # Query rate and mean service time, measured across the window rather
        # than since the last caller. A negative delta means the node restarted
        # and reset its counters, so drop the history instead of reporting the
        # nonsense spike that a backwards counter would produce.
        search = idx.get("search", {})
        q_total = search.get("query_total", 0)
        q_time = search.get("query_time_in_millis", 0)
        now = time.time()
        samples = _SEARCH_SAMPLES.setdefault(base_url, deque())
        if samples and q_total < samples[-1][1]:
            samples.clear()
        samples.append((now, q_total, q_time))
        while len(samples) > 2 and now - samples[0][0] > _RATE_WINDOW_S:
            samples.popleft()

        rate = mean_ms = None
        if len(samples) > 1:
            t0, c0, ms0 = samples[0]
            dt, dq, dms = now - t0, q_total - c0, q_time - ms0
            if dt > 0:
                rate = round(dq / dt, 1)
            if dq > 0 and dms >= 0:
                mean_ms = round(dms / dq, 1)

        # Two different questions: the process figure is how hard this engine
        # is working, the host figure includes everything else on the box —
        # which on a laptop lab is usually the load generator itself.
        os_stats, proc = node.get("os") or {}, node.get("process") or {}
        os_cpu = os_stats.get("cpu") or {}
        cpu = {
            "process_pct": (proc.get("cpu") or {}).get("percent"),
            "host_pct": os_cpu.get("percent"),
            "load1": (os_cpu.get("load_average") or {}).get("1m"),
        }

        return {
            "ts": time.time(),
            "jvm": {
                "heap_used_mb": round(jvm["mem"]["heap_used_in_bytes"] / 2**20, 1),
                "heap_max_mb": round(jvm["mem"]["heap_max_in_bytes"] / 2**20, 1),
                "gc": gc,
            },
            "cpu": cpu,
            "threads": pool,
            "breakers": {"configured": bool(breakers), "trips": breakers},
            "cores": {
                "indices (node total)": {
                    "num_docs": idx.get("docs", {}).get("count", 0),
                    "deleted_docs": idx.get("docs", {}).get("deleted", 0),
                    "warmup_ms": None,
                    # Solr counts these per replica; ES/OS report one figure
                    # for every shard the node holds, which is why the chart
                    # names its own granularity rather than claiming shards.
                    "segments": idx.get("segments", {}).get("count"),
                    "size_bytes": idx.get("store", {}).get("size_in_bytes"),
                    "caches": {
                        "queryCache": {"hitratio": ratio(qc), "size": qc.get("cache_count"),
                                       "evictions": qc.get("evictions", 0)},
                        "requestCache": {"hitratio": ratio(rc), "size": None,
                                         "evictions": rc.get("evictions", 0)},
                    },
                    "update": {
                        "adds_cumulative": idx.get("indexing", {}).get("index_total", 0),
                        "commits": idx.get("flush", {}).get("total", 0),
                        "soft_commits": idx.get("refresh", {}).get("total", 0),
                        "merges_minor": idx.get("merges", {}).get("total", 0),
                        "merges_major": 0,
                    },
                    # No percentiles on the wire: ES/OS report totals, so the
                    # honest summary of service time is the interval mean.
                    "select_p99_ms": None,
                    "select_mean_ms": mean_ms,
                    "select_rate_1m": rate,
                }
            },
        }


class ElasticsearchEngine(_EsFamily):
    name = "elasticsearch"
    node_prefix = "es"


class OpenSearchEngine(_EsFamily):
    name = "opensearch"
    node_prefix = "os"


_ENGINES = {e.name: e for e in (SolrEngine(), ElasticsearchEngine(), OpenSearchEngine())}
_ALIASES = {"es": "elasticsearch", "os": "opensearch"}


def get_engine(name: str) -> Engine:
    name = _ALIASES.get(name, name)
    if name not in _ENGINES:
        sys.exit(f"searchlab: unknown engine '{name}' — valid: {', '.join(_ENGINES)}")
    return _ENGINES[name]
