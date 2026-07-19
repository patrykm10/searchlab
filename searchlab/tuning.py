"""Live-tunable Solr settings, exposed to the control panel as plain knobs.

Built on Solr's Config API: `set-property` writes an overlay to ZooKeeper
and the affected cores reload automatically — the knob takes effect within
seconds, no restart. Only properties on Solr's editable whitelist are
offered, each described in terms of the trade-off it controls rather than
its solrconfig.xml path.

Solr-only for now; ES/OS index-settings equivalents can join later.
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
}


def registry() -> dict:
    """Knob metadata for the UI (everything except the Solr path)."""
    return {name: {k: v for k, v in spec.items() if k != "path"}
            for name, spec in KNOBS.items()}


def _dig(config: dict, dotted: str):
    cur = config
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def read_tuning(spec: ClusterSpec, collection: str, timeout: float = 15.0) -> dict:
    """Current effective knob values (in UI units) from the collection config."""
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{spec.base_url()}/{collection}/config", params={"wt": "json"})
        r.raise_for_status()
        config = r.json().get("config", {})
    out: dict[str, float | int | None] = {}
    for name, knob in KNOBS.items():
        raw = _dig(config, knob["path"])
        if isinstance(raw, (int, float)) and raw > 0:
            out[name] = round(raw / knob["scale"], 3)
        else:
            out[name] = None  # unset/disabled in config; UI shows the default
    return out


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
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{spec.base_url()}/{collection}/config",
                        json={"set-property": {knob["path"]: solr_value}})
        r.raise_for_status()
        return r.json()
