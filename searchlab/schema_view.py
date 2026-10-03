"""The live managed schema, laid out for reading rather than for Solr.

Three layers decide a field property's value: the field (or dynamic field
rule) itself, then its field type, then the type class's built-in default.
The Schema API answers "what is declared" and, with showDefaults, "what is in
effect"; comparing the two says which layer a value came from. Luke adds the
fields that actually hold data and which dynamic rule created each one, and
per-segment FieldInfos say what each segment physically contains — which is
not always what the schema says now.
"""

from __future__ import annotations

import re

import httpx

from .cluster import ClusterSpec

# properties a field or dynamic rule can set, in the order a reader wants them
FIELD_PROPS = [
    "indexed", "stored", "docValues", "multiValued", "required", "default",
    "omitNorms", "omitTermFreqAndPositions", "omitPositions",
    "termVectors", "termPositions", "termOffsets", "termPayloads",
    "storeOffsetsWithPositions", "useDocValuesAsStored", "uninvertible",
    "large", "sortMissingFirst", "sortMissingLast", "tokenized",
]

# toggles the panel offers on declared fields and dynamic rules. `tokenized`
# follows from the type class, so it is shown but not settable.
EDITABLE = {
    "indexed", "stored", "docValues", "multiValued", "required",
    "omitNorms", "omitTermFreqAndPositions", "omitPositions",
    "termVectors", "termPositions", "termOffsets", "termPayloads",
    "storeOffsetsWithPositions", "useDocValuesAsStored", "uninvertible",
    "large", "sortMissingFirst", "sortMissingLast",
}

_DV_TYPES = {"srt": "SORTED", "srs": "SORTED_SET", "num": "NUMERIC",
             "bin": "BINARY", "srn": "SORTED_NUMERIC"}


def _get(client: httpx.Client, url: str, **params) -> dict:
    r = client.get(url, params={"wt": "json", **params})
    r.raise_for_status()
    return r.json()


def _with_provenance(effective: dict, declared: dict, type_declared: dict) -> dict:
    out = {}
    for prop in FIELD_PROPS:
        if prop not in effective and prop not in declared:
            continue
        value = declared.get(prop, effective.get(prop))
        src = ("field" if prop in declared
               else "type" if prop in type_declared else "default")
        out[prop] = {"value": value, "from": src}
    return out


def parse_field_flags(flags: str) -> dict:
    """Decode one segment's FieldInfo flags, e.g. "IDsrt-OF----" or
    "-Dnum-------:1:1:4" (see the segments API's fieldInfoLegend)."""
    points = re.search(r":(\d+):(\d+):(\d+)$", flags)
    body = flags[:points.start()] if points else flags
    has_dv = body[1:2] == "D"
    dv = body[2:5] if has_dv else ""
    rest = body[5:] if has_dv else body[2:]
    return {
        "indexed": body[:1] == "I",
        "docValues": _DV_TYPES.get(dv, dv.upper()) if has_dv else None,
        "termVectors": "V" in rest,
        "omitNorms": "O" in rest,
        "omitTermFreqAndPositions": "F" in rest,
        "omitPositions": "P" in rest,
        "points": ({"dims": int(points.group(1)), "bytes": int(points.group(3))}
                   if points else None),
    }


def _match_dynamic(name: str, patterns: list[str]) -> str | None:
    # Solr prefers the longest matching pattern
    hits = [p for p in patterns
            if (p.startswith("*") and name.endswith(p[1:]))
            or (p.endswith("*") and name.startswith(p[:-1]))]
    return max(hits, key=len) if hits else None


def one_replica_per_shard(state: dict) -> list[tuple[str, str, int]]:
    """(shard, core, node index) for one active replica of each active shard,
    the leader when there is one. Luke and the segments API answer for a
    single core, so asking the collection covers whichever replica Solr
    happened to route to; on a multi-shard collection that is part of the
    data, and a Reload could land on a different part."""
    out = []
    for shard, sh in sorted((state.get("shards") or {}).items()):
        if sh.get("state", "active") != "active":
            continue
        reps = [r for r in (sh.get("replicas") or {}).values() if r.get("state") == "active"]
        if not reps:
            continue
        rep = next((r for r in reps if str(r.get("leader")) == "true"), reps[0])
        node = (rep.get("node_name") or "").split(":")[0]
        digits = "".join(ch for ch in node if ch.isdigit())
        out.append((shard, rep.get("core", ""), max(0, int(digits) - 1) if digits else 0))
    return out


def read_schema(spec: ClusterSpec, collection: str, timeout: float = 20.0) -> dict:
    base = f"{spec.base_url()}/{collection}"
    with httpx.Client(timeout=timeout) as client:
        declared = _get(client, f"{base}/schema")["schema"]
        eff_fields = _get(client, f"{base}/schema/fields", showDefaults="true")["fields"]
        eff_dyn = _get(client, f"{base}/schema/dynamicfields",
                       showDefaults="true")["dynamicFields"]
        eff_types = _get(client, f"{base}/schema/fieldtypes",
                         showDefaults="true")["fieldTypes"]
        colls = (_get(client, f"{spec.base_url()}/admin/collections", action="CLUSTERSTATUS")
                 .get("cluster", {}).get("collections", {}))
        replicas = one_replica_per_shard(colls.get(collection) or {})
        multi = len(replicas) > 1
        luke: dict[str, dict] = {}
        segs: dict[str, dict] = {}
        # every shard's data, summed: document counts add up, and segment
        # names repeat across shards, so they carry the shard's name
        for shard, core, node in replicas or [("", collection, 0)]:
            url = f"{spec.base_url(node)}/{core}"
            for name, info in _get(client, f"{url}/admin/luke", numTerms=0).get("fields", {}).items():
                if name in luke:
                    luke[name]["docs"] = (luke[name].get("docs") or 0) + (info.get("docs") or 0)
                else:
                    luke[name] = dict(info)
            for name, seg in _get(client, f"{url}/admin/segments",
                                  fieldInfo="true").get("segments", {}).items():
                segs[f"{shard} {name}" if multi else name] = seg

    # a schema belongs to a configset, not a collection: an edit made
    # through one collection changes every collection sharing it
    configset = (colls.get(collection) or {}).get("configName")
    shared_with = sorted(n for n, c in colls.items()
                         if n != collection and c.get("configName") == configset)

    type_decl = {t["name"]: t for t in declared.get("fieldTypes", [])}
    field_decl = {f["name"]: f for f in declared.get("fields", [])}
    dyn_decl = {f["name"]: f for f in declared.get("dynamicFields", [])}

    def entry(eff: dict, decl: dict, kind: str) -> dict:
        tdecl = type_decl.get(eff.get("type"), {})
        return {"name": eff["name"], "type": eff.get("type"), "kind": kind,
                "declared": sorted(k for k in decl if k not in ("name", "type")),
                "props": _with_provenance(eff, decl, tdecl)}

    fields = [entry(f, field_decl.get(f["name"], {}), "field") for f in eff_fields]
    dynamic = [entry(f, dyn_decl.get(f["name"], {}), "dynamic") for f in eff_dyn]
    by_field = {f["name"]: f for f in fields}
    by_dyn = {f["name"]: f for f in dynamic}

    # what each segment physically holds, per field
    on_disk: dict[str, list[dict]] = {}
    for seg_name, seg in segs.items():
        for fname, info in (seg.get("fields") or {}).items():
            on_disk.setdefault(fname, []).append({
                "segment": seg_name, "flags": info.get("flags", ""),
                **parse_field_flags(info.get("flags", "")),
                "docs": info.get("docCount"), "terms": info.get("termCount")})

    in_index = []
    for name, info in sorted(luke.items()):
        base_rule = info.get("dynamicBase")
        if name in by_field:
            source = {"kind": "field", "name": name}
            props = by_field[name]["props"]
        else:
            rule = base_rule or _match_dynamic(name, list(by_dyn))
            source = {"kind": "dynamic", "name": rule}
            props = by_dyn.get(rule, {}).get("props", {})
        in_index.append({"name": name, "type": info.get("type"), "docs": info.get("docs"),
                         "source": source, "props": props,
                         "segments": sorted(on_disk.get(name, []), key=lambda s: s["segment"])})

    for d in dynamic:
        d["matches"] = [f["name"] for f in in_index
                        if f["source"] == {"kind": "dynamic", "name": d["name"]}]

    used_types = {f["type"] for f in in_index}
    types = []
    for t in eff_types:
        decl = type_decl.get(t["name"], {})
        types.append({
            "name": t["name"], "class": t.get("class"),
            "declared": sorted(k for k in decl if k not in ("name", "class")),
            "props": {k: {"value": v, "from": "type" if k in decl else "default"}
                      for k, v in t.items()
                      if k not in ("name", "class") and not isinstance(v, (dict, list))},
            "analyzers": {k: t[k] for k in ("analyzer", "indexAnalyzer", "queryAnalyzer",
                                            "multiTermAnalyzer") if k in t},
            "similarity": t.get("similarity"),
            "in_use": t["name"] in used_types,
            "used_by": sorted(f["name"] for f in in_index if f["type"] == t["name"]),
        })

    return {
        "name": declared.get("name"), "version": declared.get("version"),
        "uniqueKey": declared.get("uniqueKey"), "similarity": declared.get("similarity"),
        "configset": configset, "shared_with": shared_with,
        "in_index": in_index, "fields": fields, "dynamic_fields": dynamic,
        "field_types": types, "copy_fields": declared.get("copyFields", []),
        "segments": sorted(segs),
        "read_from": [core for _, core, _ in replicas],
    }


def set_field_property(spec: ClusterSpec, collection: str, kind: str, name: str,
                       prop: str, value: bool, dry_run: bool = False,
                       timeout: float = 20.0) -> dict:
    """Change one property on a declared field or dynamic rule.

    The command replaces the whole definition as declared — not the
    effective one, which would pin every inherited default into the schema.
    """
    if prop not in EDITABLE:
        raise ValueError(f"{prop} cannot be set from here")
    if kind not in ("field", "dynamic"):
        raise ValueError("kind must be field or dynamic")
    base = f"{spec.base_url()}/{collection}/schema"
    path = "fields" if kind == "field" else "dynamicfields"
    key = "field" if kind == "field" else "dynamicField"
    with httpx.Client(timeout=timeout) as client:
        r = client.get(f"{base}/{path}/{name}", params={"wt": "json"})
        if r.status_code == 404:
            raise ValueError(f"no {kind} named {name}")
        r.raise_for_status()
        current = r.json()[key]
        new = {**current, prop: bool(value)}
        # setting a value the type would give anyway removes the override,
        # so flipping something back restores the definition as it was
        t = client.get(f"{base}/fieldtypes/{current['type']}",
                       params={"wt": "json", "showDefaults": "true"})
        t.raise_for_status()
        if t.json().get("fieldType", {}).get(prop) == bool(value):
            new.pop(prop)
        command = {("replace-field" if kind == "field" else "replace-dynamic-field"): new}
        if dry_run:
            return {"command": command}
        r = client.post(base, params={"wt": "json"}, json=command)
        body = r.json()
        if r.status_code != 200 or body.get("errors") or body.get("error"):
            err = body.get("errors") or body.get("error")
            raise ValueError(f"Solr refused the change: {err}")
    return {"command": command}
