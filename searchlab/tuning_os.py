"""Live-tunable OpenSearch / Elasticsearch index settings.

The same knobs the Solr side exposes, pointed at the equivalent index
settings. The mechanism is simpler here and worth noting: these are
*dynamic* settings, updatable with a single PUT to `<index>/_settings`,
taking effect immediately with no reload and no restart.

That matters because on Solr the merge-policy knobs are not on the Config
API's editable whitelist at all, which is why that side needs a patched
configset with ${searchlab.*} placeholders to make them reachable. Here
they are first-class. Same UI, noticeably less machinery underneath.

Values are exchanged in human units (seconds, MB, percent) and converted
at the boundary, because the API speaks several different notations for
what is conceptually one number: "30s", "512mb", "5368709120b", 20.0.
"""

from __future__ import annotations

import re

import httpx

from .cluster import ClusterSpec

# name -> knob. `path` is the index setting, minus the "index." prefix.
# `kind` says how to render the value for the API and read it back:
#   seconds  -> "30s"      size_mb -> "512mb"      number -> 4 / 20.0
KNOBS: dict[str, dict] = {
    "refresh_s": {
        "path": "refresh_interval", "kind": "seconds",
        "label": "Search visibility delay",
        "setting": "index.refresh_interval",
        "desc": "How long until newly added documents show up in searches. "
                "Shorter feels more real-time but costs indexing throughput "
                "and throws away caches more often; longer is easier on the "
                "cluster. The direct counterpart of Solr's soft commit.",
        "unit": "s", "min": 1, "max": 300, "default": 1,
    },
    "translog_mb": {
        "path": "translog.flush_threshold_size", "kind": "size_mb",
        "label": "Save-to-disk threshold",
        "setting": "index.translog.flush_threshold_size",
        "desc": "How much un-flushed data may accumulate in the translog "
                "before a Lucene commit is forced. Larger means fewer, "
                "bigger flushes; smaller means more frequent disk churn.",
        "unit": "MB", "min": 16, "max": 4096, "default": 512,
    },
    "replicas": {
        "path": "number_of_replicas", "kind": "number",
        "label": "Copies per shard",
        "setting": "index.number_of_replicas",
        "desc": "Extra copies of every shard. Unlike Solr, this is a plain "
                "setting rather than a per-replica operation — raising it "
                "makes the cluster create the copies for you, and they cost "
                "indexing throughput as well as disk.",
        "unit": "copies", "min": 0, "max": 4, "default": 1,
    },
    "segments_per_tier": {
        "path": "merge.policy.segments_per_tier", "kind": "number",
        "label": "Merge threshold",
        "setting": "index.merge.policy.segments_per_tier",
        "desc": "How many segments are allowed to accumulate before they get "
                "merged. Lower keeps the index compact and searches fast but "
                "burns more CPU and disk on merging.",
        "unit": "segments", "min": 2, "max": 50, "default": 10,
    },
    "max_merged_mb": {
        "path": "merge.policy.max_merged_segment", "kind": "size_mb",
        "label": "Largest merged segment",
        "setting": "index.merge.policy.max_merged_segment",
        "desc": "The biggest segment the merger will produce. Smaller spreads "
                "the index over more segments; bigger concentrates it but "
                "makes the heaviest merges heavier.",
        "unit": "MB", "min": 256, "max": 20480, "default": 5120,
    },
    "deletes_pct": {
        "path": "merge.policy.deletes_pct_allowed", "kind": "number",
        "label": "Deleted-document tolerance",
        "setting": "index.merge.policy.deletes_pct_allowed",
        "desc": "What share of the index may be dead (updated or deleted) "
                "documents before merging reclaims them. Lower keeps the "
                "index lean; higher defers the cleanup cost.",
        "unit": "%", "min": 5, "max": 50, "default": 20,
    },
    "merge_threads": {
        "path": "merge.scheduler.max_thread_count", "kind": "number",
        "label": "Merge threads",
        "setting": "index.merge.scheduler.max_thread_count",
        "desc": "How many merges may run at once. More finishes merging "
                "sooner but competes with indexing and queries for CPU and "
                "disk. Solr has no per-index equivalent of this knob.",
        "unit": "threads", "min": 1, "max": 8, "default": 4,
    },
    "result_window": {
        "path": "max_result_window", "kind": "number",
        "label": "Deep paging limit",
        "setting": "index.max_result_window",
        "desc": "How far into a result set a plain from/size query may reach. "
                "Raising it makes deep paging possible and expensive — each "
                "page has to rank everything before it.",
        "unit": "docs", "min": 1000, "max": 200000, "default": 10000,
    },
}

_META = ("path", "kind")
_SIZE = re.compile(r"^([\d.]+)\s*(b|kb|mb|gb|tb)?$", re.I)
_MULT = {"b": 1 / 2**20, "kb": 1 / 1024, "mb": 1.0, "gb": 1024.0, "tb": 1024.0 * 1024}


def registry(names=None) -> dict:
    """Knob metadata for the UI, with internal wiring stripped."""
    return {name: {k: v for k, v in spec.items() if k not in _META}
            for name, spec in KNOBS.items()
            if names is None or name in names}


def _to_mb(raw) -> float | None:
    """'512mb' / '5368709120b' / 1048576 -> megabytes."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(raw / 2**20, 2)
    m = _SIZE.match(str(raw).strip())
    if not m:
        return None
    value, unit = float(m.group(1)), (m.group(2) or "b").lower()
    return round(value * _MULT[unit], 2)


def _to_seconds(raw) -> float | None:
    """'30s' / '1m' / '500ms' -> seconds. -1 means refresh disabled."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("-1", "-1s"):
        return None                       # disabled, not a number to show
    m = re.match(r"^([\d.]+)\s*(ms|s|m|h)?$", text)
    if not m:
        return None
    value, unit = float(m.group(1)), (m.group(2) or "s")
    return round(value * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit], 3)


def _read(kind: str, raw):
    if kind == "seconds":
        return _to_seconds(raw)
    if kind == "size_mb":
        return _to_mb(raw)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _write(kind: str, value: float):
    """Render a UI value in the notation the settings API expects."""
    if kind == "seconds":
        return f"{value:g}s"
    if kind == "size_mb":
        return f"{value:g}mb"
    return int(value) if float(value).is_integer() else value


def tuning_state(spec: ClusterSpec, index: str, timeout: float = 15.0) -> dict:
    """Current knob values, read with defaults included.

    `include_defaults` matters: a setting never explicitly set is absent
    from the index's own settings block, and showing it as blank would
    suggest it is unset rather than at its default.
    """
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{index}/_settings",
                       params={"include_defaults": "true", "flat_settings": "true"})
        r.raise_for_status()
        body = r.json()
    block = body.get(index) or next(iter(body.values()), {})
    merged = {**(block.get("defaults") or {}), **(block.get("settings") or {})}

    values = {}
    for name, knob in KNOBS.items():
        values[name] = _read(knob["kind"], merged.get(f"index.{knob['path']}"))
    reg = registry()
    url = verify_url(spec, index)
    for item in reg.values():
        item["verify"] = url          # one endpoint shows every setting
    return {"values": values, "registry": reg}


def apply_tuning(spec: ClusterSpec, index: str, name: str, value: float,
                 timeout: float = 30.0) -> dict:
    """Validate a knob turn and PUT it to the index settings."""
    knob = KNOBS.get(name)
    if knob is None:
        raise ValueError(f"Unknown setting: {name}")
    if not knob["min"] <= value <= knob["max"]:
        raise ValueError(
            f"{knob['label']} must be between {knob['min']} and "
            f"{knob['max']} {knob['unit']}.")
    payload = {"index": {knob["path"]: _write(knob["kind"], value)}}
    with httpx.Client(timeout=timeout) as client:
        r = client.put(f"{spec.base_url()}/{index}/_settings", json=payload)
        if r.status_code >= 400:
            # the API explains refusals well; pass that through rather than
            # a bare status code
            try:
                msg = r.json().get("error", {}).get("reason") or r.text[:300]
            except ValueError:
                msg = r.text[:300]
            raise RuntimeError(msg)
        return r.json()


def verify_url(spec: ClusterSpec, index: str) -> str:
    """Where to see these values in the engine itself."""
    return f"{spec.base_url()}/{index}/_settings?include_defaults=true&flat_settings=true"
