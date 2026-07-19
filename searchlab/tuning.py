"""Live-tunable Solr settings, exposed to the control panel as plain knobs.

Two mechanisms, one UX:

- Whitelist knobs use the Config API's `set-property` (commit timing, cache
  sizes/autowarm) — Solr writes an overlay to ZooKeeper and reloads the
  affected cores automatically.
- Merge/indexing knobs (`user_prop: True`) aren't on Solr's editable
  whitelist, so the lab's configset parameterizes them as ${searchlab.*}
  references in solrconfig.xml (see configset.py) and `set-user-property`
  makes them live the same way. On collections created without the lab
  configset these knobs are omitted from the API response rather than
  silently doing nothing.

Either way: knob turn -> overlay in ZK -> automatic core reload -> live in
seconds, no restart. Solr-only for now; ES/OS equivalents can join later.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

# name -> knob spec. `path` is the Config API property; `scale` converts
# UI units to Solr units (UI seconds -> ms for the commit knobs).
KNOBS: dict[str, dict] = {
    "soft_commit_s": {
        "path": "updateHandler.autoSoftCommit.maxTime",
        "label": "Search visibility delay",
        "desc": "How long until newly added documents show up in searches. "
                "Shorter feels more real-time but costs indexing and cache "
                "performance; longer is easier on the cluster.",
        "unit": "s", "min": 1, "max": 60, "default": 3, "scale": 1000,
    },
    "hard_commit_s": {
        "path": "updateHandler.autoCommit.maxTime",
        "label": "Save-to-disk interval",
        "desc": "How often changes are durably written to disk. Rarely needs "
                "changing; very short intervals cause heavy disk churn.",
        "unit": "s", "min": 5, "max": 300, "default": 15, "scale": 1000,
    },
    "filter_cache": {
        "path": "query.filterCache.size",
        "label": "Filter cache size",
        "desc": "How many filter results are kept for reuse. Bigger helps "
                "repeated filtering (categories, price ranges) at the cost "
                "of memory.",
        "unit": "entries", "min": 64, "max": 8192, "default": 512, "scale": 1,
    },
    "result_cache": {
        "path": "query.queryResultCache.size",
        "label": "Result cache size",
        "desc": "How many whole result pages are kept for reuse. Helps when "
                "the same searches repeat; costs memory.",
        "unit": "entries", "min": 64, "max": 8192, "default": 512, "scale": 1,
    },
    "doc_cache": {
        "path": "query.documentCache.size",
        "label": "Document cache size",
        "desc": "How many fetched documents are kept in memory. Helps when "
                "the same documents appear in many result pages.",
        "unit": "entries", "min": 64, "max": 8192, "default": 512, "scale": 1,
    },
    "filter_autowarm": {
        "path": "query.filterCache.autowarmCount",
        "label": "Cache pre-warming",
        "desc": "How many filter-cache entries are recomputed after each "
                "commit. More keeps queries fast right after a commit, but "
                "makes each commit slower.",
        "unit": "entries", "min": 0, "max": 512, "default": 0, "scale": 1,
    },
    "commit_max_docs": {
        "path": "updateHandler.autoCommit.maxDocs",
        "label": "Save after N documents",
        "desc": "Also save to disk once this many new documents pile up, "
                "regardless of the time interval. Useful under heavy "
                "indexing bursts.",
        "unit": "docs", "min": 1000, "max": 1000000, "default": 25000, "scale": 1,
    },
    # ---- merge/indexing knobs: need the lab configset (user properties) ----
    "ram_buffer_mb": {
        "path": "searchlab.ramBufferMB", "user_prop": True,
        "config_probe": "indexConfig.ramBufferSizeMB",
        "label": "Indexing memory buffer",
        "desc": "How much indexed data is held in memory before being "
                "written out as a new segment. Bigger means fewer, larger "
                "segments and less merge churn — at the cost of memory.",
        "unit": "MB", "min": 16, "max": 2048, "default": 100, "scale": 1,
    },
    "segments_per_tier": {
        "path": "searchlab.segmentsPerTier", "user_prop": True,
        "config_probe": "indexConfig.mergePolicyFactory",
        "label": "Merge threshold",
        "desc": "How many segments are allowed to accumulate before they "
                "get merged. Lower keeps the index compact and searches "
                "fast but burns more CPU and disk on merging.",
        "unit": "segments", "min": 2, "max": 50, "default": 10, "scale": 1,
    },
    "max_merged_mb": {
        "path": "searchlab.maxMergedSegMB", "user_prop": True,
        "config_probe": "indexConfig.mergePolicyFactory",
        "label": "Largest merged segment",
        "desc": "The biggest segment the merger will create. Smaller spreads "
                "the index over more segments; bigger concentrates it but "
                "makes the heaviest merges heavier.",
        "unit": "MB", "min": 256, "max": 20480, "default": 5120, "scale": 1,
    },
    "deletes_pct": {
        "path": "searchlab.deletesPctAllowed", "user_prop": True,
        "config_probe": "indexConfig.mergePolicyFactory",
        "label": "Deleted-document tolerance",
        "desc": "What share of the index may be dead (updated or deleted) "
                "documents before merging cleans them out. Lower keeps the "
                "index lean; higher defers the cleanup cost.",
        "unit": "%", "min": 20, "max": 50, "default": 33, "scale": 1,
    },
}


_META_KEYS = ("path", "user_prop", "config_probe")


def registry(names=None) -> dict:
    """Knob metadata for the UI (internal wiring keys stripped)."""
    return {name: {k: v for k, v in spec.items() if k not in _META_KEYS}
            for name, spec in KNOBS.items()
            if names is None or name in names}


def _dig(config: dict, dotted: str):
    cur = config
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _knob_available(knob: dict, config: dict) -> bool:
    """User-prop knobs only work when the lab configset parameterized them."""
    if not knob.get("user_prop"):
        return True
    return _dig(config, knob["config_probe"]) is not None


def tuning_state(spec: ClusterSpec, collection: str, timeout: float = 15.0) -> dict:
    """Current values + registry for the knobs this collection supports."""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{collection}/config", params={"wt": "json"})
        r.raise_for_status()
        config = r.json().get("config", {})
        try:
            r = client.get(f"{spec.base_url()}/{collection}/config/overlay",
                           params={"wt": "json"})
            r.raise_for_status()
            userprops = r.json().get("overlay", {}).get("userProps", {})
        except httpx.HTTPError:
            userprops = {}

    values: dict[str, float | int | None] = {}
    merge_missing = False
    for name, knob in KNOBS.items():
        if not _knob_available(knob, config):
            merge_missing = True
            continue
        raw = userprops.get(knob["path"]) if knob.get("user_prop") \
            else _dig(config, knob["path"])
        if isinstance(raw, str):
            try:
                raw = float(raw)
            except ValueError:
                raw = None
        if isinstance(raw, (int, float)) and raw > 0:
            values[name] = round(raw / knob["scale"], 3)
        else:
            values[name] = None  # unset/disabled; UI shows the default
    out = {"values": values, "registry": registry(values.keys())}
    if merge_missing:
        out["note"] = ("Merge and indexing-buffer knobs need a collection created "
                       "by this version of searchlab — recreate the collection to "
                       "enable them.")
    return out


def read_tuning(spec: ClusterSpec, collection: str, timeout: float = 15.0) -> dict:
    """Current effective knob values (in UI units); see tuning_state."""
    return tuning_state(spec, collection, timeout)["values"]


def apply_tuning(spec: ClusterSpec, collection: str, name: str, value: float,
                 timeout: float = 30.0) -> dict:
    """Validate a knob turn and write it through the Config API."""
    knob = KNOBS.get(name)
    if knob is None:
        raise ValueError(f"Unknown setting: {name}")
    if not knob["min"] <= value <= knob["max"]:
        raise ValueError(
            f"{knob['label']} must be between {knob['min']} and {knob['max']} {knob['unit']}.")
    solr_value = int(value * knob["scale"])
    command = "set-user-property" if knob.get("user_prop") else "set-property"
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{spec.base_url()}/{collection}/config",
                        json={command: {knob["path"]: solr_value}})
        r.raise_for_status()
        return r.json()
