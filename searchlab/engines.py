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

class _EsFamily(Engine):
    """Shared behavior for Elasticsearch and OpenSearch (same wire APIs)."""

    compose_template = "docker-compose-es.yml.j2"

    def base_url(self, spec, node: int = 0) -> str:
        return f"http://localhost:{spec.base_port + node}"

    def health_ok(self, client, spec, node):
        return client.get(self.base_url(spec, node)).status_code == 200

    def create_index(self, spec, name, shards, replicas, config_set="_default"):
        settings = {"number_of_shards": shards,
                    "number_of_replicas": max(replicas - 1, 0)}
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
            stats = client.get(f"{base_url}/_nodes/_local/stats/jvm,indices").json()
        node = next(iter(stats["nodes"].values()))
        jvm, idx = node["jvm"], node["indices"]
        gc = {name: {"count": c.get("collection_count", 0),
                     "time": c.get("collection_time_in_millis", 0)}
              for name, c in jvm.get("gc", {}).get("collectors", {}).items()}
        qc = idx.get("query_cache", {})
        rc = idx.get("request_cache", {})

        def ratio(d):
            h, m = d.get("hit_count", 0), d.get("miss_count", 0)
            return round(h / (h + m), 3) if h + m else None

        return {
            "ts": time.time(),
            "jvm": {
                "heap_used_mb": round(jvm["mem"]["heap_used_in_bytes"] / 2**20, 1),
                "heap_max_mb": round(jvm["mem"]["heap_max_in_bytes"] / 2**20, 1),
                "gc": gc,
            },
            "cores": {
                "indices (node total)": {
                    "num_docs": idx.get("docs", {}).get("count", 0),
                    "deleted_docs": idx.get("docs", {}).get("deleted", 0),
                    "warmup_ms": None,
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
                    "select_p99_ms": None,   # ES exposes totals, not percentiles
                    "select_rate_1m": None,
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
