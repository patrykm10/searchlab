"""The live solrconfig, laid out for reading, with the Config API's edits.

Three places a value can come from: solrconfig.xml itself; the configset's
overlay (configoverlay.json), which is where the Config API's set-property
writes; and, for the lab configset's merge/buffer settings, a
${searchlab.*} user property substituted into solrconfig.xml (see
configset.py). The Config API reports the effective result; the overlay
says which values it changed.
"""

from __future__ import annotations

import httpx

from .cluster import ClusterSpec

# Properties Solr's Config API accepts in set-property. Verified against Solr
# 9.6 by setting each to its current value: all of these were accepted, and
# updateHandler.indexWriter.closeWaitsForMerges, indexConfig.*,
# directoryFactory.* and the update log were refused.
EDITABLE = frozenset({
    "updateHandler.autoCommit.maxDocs", "updateHandler.autoCommit.maxTime",
    "updateHandler.autoCommit.openSearcher",
    "updateHandler.autoSoftCommit.maxDocs", "updateHandler.autoSoftCommit.maxTime",
    "updateHandler.commitWithin.softCommit",
    "query.filterCache.size", "query.filterCache.initialSize",
    "query.filterCache.autowarmCount", "query.filterCache.maxRamMB",
    "query.queryResultCache.size", "query.queryResultCache.autowarmCount",
    "query.queryResultCache.maxRamMB",
    "query.documentCache.size", "query.documentCache.autowarmCount",
    "query.fieldValueCache.size", "query.fieldValueCache.autowarmCount",
    "query.useFilterForSortedQuery", "query.queryResultWindowSize",
    "query.queryResultMaxDocsCached", "query.enableLazyFieldLoading",
    "query.boolTofilterOptimizer", "query.maxBooleanClauses",
    "requestDispatcher.handleSelect",
    "requestDispatcher.requestParsers.multipartUploadLimitInKB",
    "requestDispatcher.requestParsers.formdataUploadLimitInKB",
    "requestDispatcher.requestParsers.addHttpRequestToContext",
    "requestDispatcher.requestParsers.enableRemoteStreaming",
    "requestDispatcher.requestParsers.enableStreamBody",
})

# indexConfig is not editable through set-property, so the lab configset
# writes these as ${searchlab.*} references (configset.INDEX_CONFIG_BLOCK)
USER_PROPS = {
    "indexConfig.ramBufferSizeMB": ("searchlab.ramBufferMB", 100),
    "indexConfig.mergePolicyFactory.segmentsPerTier": ("searchlab.segmentsPerTier", 10),
    "indexConfig.mergePolicyFactory.maxMergedSegmentMB": ("searchlab.maxMergedSegMB", 5120),
    "indexConfig.mergePolicyFactory.deletesPctAllowed": ("searchlab.deletesPctAllowed", 33),
}


def _get(client: httpx.Client, url: str, **params) -> dict:
    r = client.get(url, params={"wt": "json", "omitHeader": "true", **params})
    r.raise_for_status()
    return r.json()


def _leaves(node, prefix: str = "") -> dict:
    out = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else k))
    else:
        out[prefix] = node
    return out


def _lookup(tree: dict, path: str):
    node = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def read_config(spec: ClusterSpec, collection: str, timeout: float = 20.0) -> dict:
    base = f"{spec.base_url()}/{collection}"
    with httpx.Client(timeout=timeout) as client:
        cfg = _get(client, f"{base}/config")["config"]
        overlay = _get(client, f"{base}/config/overlay").get("overlay", {})
        colls = (_get(client, f"{spec.base_url()}/admin/collections", action="CLUSTERSTATUS")
                 .get("cluster", {}).get("collections", {}))
    cfg.pop("znodeVersion", None)
    user_props = overlay.get("userProps") or {}
    configset = (colls.get(collection) or {}).get("configName")
    # the lab's substitutions only exist in the lab configset
    lab = {path: {"prop": prop, "file_default": dflt, "set": prop in user_props,
                  "value": user_props.get(prop)}
           for path, (prop, dflt) in USER_PROPS.items()
           if _lookup(cfg, path) is not None and configset == "searchlab"}
    return {
        "config": cfg,
        "overlay": sorted(_leaves(overlay.get("props") or {})),
        "user_props": user_props,
        "lab_props": lab,
        # includes properties absent from solrconfig.xml: they can still be set
        "editable": sorted(EDITABLE),
        "configset": configset,
        "shared_with": sorted(n for n, c in colls.items()
                              if n != collection and c.get("configName") == configset),
    }


def _is_num(v) -> bool:
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False


def _coerce(current, value):
    """Type a new value like the one it replaces (Solr reports cache sizes as
    strings); with nothing to go by, like it reads."""
    text = str(value).strip()
    if isinstance(current, bool) or text.lower() in ("true", "false"):
        if isinstance(value, bool):
            return value
        if text.lower() not in ("true", "false"):
            raise ValueError(f"expected true or false, got {value!r}")
        return text.lower() == "true"
    if _is_num(current) or (current is None and _is_num(text)):
        if not _is_num(text):
            raise ValueError(f"expected a number, got {value!r}")
        num = float(text)
        return int(num) if num == int(num) else num
    return value


def config_command(spec: ClusterSpec, collection: str, path: str, value, reset: bool,
                   timeout: float = 20.0) -> dict:
    """The Config API command that sets (or resets) one property."""
    if path in USER_PROPS:
        prop = USER_PROPS[path][0]
        if reset:
            return {"unset-user-property": prop}
        return {"set-user-property": {prop: _coerce(USER_PROPS[path][1], value)}}
    if path not in EDITABLE:
        raise ValueError(f"{path} cannot be changed through the Config API")
    if reset:
        return {"unset-property": path}
    with httpx.Client(timeout=timeout) as client:
        cfg = _get(client, f"{spec.base_url()}/{collection}/config")["config"]
    return {"set-property": {path: _coerce(_lookup(cfg, path), value)}}


def set_config(spec: ClusterSpec, collection: str, path: str, value=None,
               reset: bool = False, dry_run: bool = False, timeout: float = 30.0) -> dict:
    command = config_command(spec, collection, path, value, reset)
    if dry_run:
        return {"command": command}
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{spec.base_url()}/{collection}/config",
                        params={"wt": "json"}, json=command)
        body = r.json()
    if r.status_code != 200 or body.get("errorMessages") or body.get("error"):
        raise ValueError(f"Solr refused the change: "
                         f"{body.get('errorMessages') or body.get('error')}")
    return {"command": command}
