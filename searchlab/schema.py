"""Derive an explicit Solr schema from a searchlab data profile.

The `_default` configset's dynamic fields (`*_t`, `*_s`, `*_i`, ...) work fine
for quick labs, but explicit fields let you control docValues and indexing per
field — which is exactly what many performance repros hinge on (e.g. faceting
on a field without docValues). Profile fields may carry a `solr:` block with
overrides:

    user_id_s:
      type: keyword
      solr: { docValues: false }   # reproduce fieldCache pressure
"""

from __future__ import annotations

import json
import sys

import httpx

from .cluster import ClusterSpec

_TYPE_MAP = {
    "text": "text_general",
    "keyword": "string",
    "categorical": "string",
    "int": "pint",
    "float": "pfloat",
    "date": "pdate",
    "bool": "boolean",
}

_ES_TYPE_MAP = {
    "text": "text",
    "keyword": "keyword",
    "categorical": "keyword",
    "int": "integer",
    "float": "float",
    "date": "date",
    "bool": "boolean",
}


def mappings_from_profile(profile: dict, engine: str = "elasticsearch") -> dict:
    """Build ES/OS mapping properties. All ES fields accept arrays natively,
    so `multivalued` just maps its inner type. Per-field `es:` blocks override
    (e.g. `es: {doc_values: false}` to reproduce fielddata pressure)."""
    props: dict = {}
    for name, cfg in profile["fields"].items():
        cfg = cfg or {}
        ftype = cfg.get("type", "text")
        if ftype == "id":
            continue  # _id is metadata in ES
        if ftype == "vector":
            dims = int(cfg.get("dims", 768))
            sim = cfg.get("similarity", "cosine")
            if engine == "opensearch":
                field = {"type": "knn_vector", "dimension": dims,
                         "method": {"name": "hnsw",
                                    "space_type": {"cosine": "cosinesimil",
                                                   "dot_product": "innerproduct",
                                                   "euclidean": "l2"}.get(sim, "cosinesimil"),
                                    "engine": "lucene"}}
            else:
                field = {"type": "dense_vector", "dims": dims,
                         "index": True, "similarity": sim}
            field.update(cfg.get("es", {}))
            props[name] = field
            continue
        inner = cfg.get("of", {"type": "keyword"}) if ftype == "multivalued" else cfg
        es_type = _ES_TYPE_MAP.get(inner.get("type", "keyword"))
        if es_type is None:
            sys.exit(f"searchlab: field '{name}' has type '{inner.get('type')}' with no ES mapping")
        field: dict = {"type": es_type}
        if es_type == "date":
            field["format"] = "strict_date_time_no_millis||strict_date_optional_time"
        field.update(cfg.get("es", {}))
        props[name] = field
    return {"properties": props}


def apply_mappings(spec, collection: str, profile: dict, dry_run: bool = False,
                   engine: str = "elasticsearch") -> str:
    mappings = mappings_from_profile(profile, engine=engine)
    if dry_run:
        return "would apply:\n" + json.dumps(mappings, indent=2)
    r = httpx.put(f"{spec.base_url()}/{collection}/_mapping", json=mappings, timeout=60)
    if r.status_code != 200:
        sys.exit(f"searchlab: mapping update failed: {r.text[:400]}")
    return f"applied mappings for {len(mappings['properties'])} field(s): " \
           f"{', '.join(mappings['properties'])}"


def vector_field_types(profile: dict) -> list[dict]:
    """Solr needs a DenseVectorField fieldType per (dims, similarity) combo
    before fields can reference it. HNSW knobs come from the `solr:` block."""
    types: dict[str, dict] = {}
    for _, cfg in profile["fields"].items():
        cfg = cfg or {}
        if cfg.get("type") != "vector":
            continue
        dims = int(cfg.get("dims", 768))
        sim = cfg.get("similarity", "cosine")
        tname = f"knn_vector_{dims}_{sim}"
        ft = {"name": tname, "class": "solr.DenseVectorField",
              "vectorDimension": dims, "similarityFunction": sim}
        for knob in ("hnswMaxConnections", "hnswBeamWidth"):
            if knob in cfg.get("solr", {}):
                ft[knob] = cfg["solr"][knob]
        types[tname] = ft
    return list(types.values())


def fields_from_profile(profile: dict) -> list[dict]:
    """Build Schema API field definitions from a profile's fields section."""
    defs = []
    for name, cfg in profile["fields"].items():
        cfg = cfg or {}
        ftype = cfg.get("type", "text")
        if ftype == "id":
            continue  # uniqueKey `id` already exists in every configset
        if ftype == "vector":
            dims = int(cfg.get("dims", 768))
            sim = cfg.get("similarity", "cosine")
            field = {"name": name, "type": f"knn_vector_{dims}_{sim}",
                     "indexed": True, "stored": True}
            field.update({k: v for k, v in cfg.get("solr", {}).items()
                          if not k.startswith("hnsw")})
            defs.append(field)
            continue
        multi = ftype == "multivalued"
        inner = cfg.get("of", {"type": "keyword"}) if multi else cfg
        solr_type = _TYPE_MAP.get(inner.get("type", "keyword"))
        if solr_type is None:
            sys.exit(f"searchlab: field '{name}' has type '{inner.get('type')}' with no Solr mapping")
        field = {
            "name": name,
            "type": solr_type,
            "indexed": True,
            "stored": True,
            "multiValued": multi,
        }
        # string/numeric/date default to docValues (facet/sort without fieldCache)
        if solr_type != "text_general":
            field["docValues"] = True
        field.update(cfg.get("solr", {}))
        defs.append(field)
    return defs


def apply_schema(spec: ClusterSpec, collection: str, profile: dict, dry_run: bool = False) -> str:
    """Add missing fields; replace existing ones whose definition differs."""
    defs = fields_from_profile(profile)
    if dry_run:
        payload = {"add-field": defs}
        ftypes = vector_field_types(profile)
        if ftypes:
            payload = {"add-field-type": ftypes, **payload}
        return "would apply:\n" + json.dumps(payload, indent=2)

    schema_url = f"{spec.base_url()}/{collection}/schema"
    with httpx.Client(timeout=60) as client:
        # Vector fields depend on their fieldTypes existing first.
        ftypes = vector_field_types(profile)
        if ftypes:
            r = client.get(f"{schema_url}/fieldtypes", params={"wt": "json"})
            r.raise_for_status()
            have = {t["name"] for t in r.json().get("fieldTypes", [])}
            missing = [t for t in ftypes if t["name"] not in have]
            if missing:
                r = client.post(schema_url, params={"wt": "json"},
                                json={"add-field-type": missing})
                if r.status_code != 200 or r.json().get("errors"):
                    sys.exit(f"searchlab: field-type create failed: {r.text[:400]}")

        r = client.get(f"{schema_url}/fields", params={"wt": "json"})
        r.raise_for_status()
        existing = {f["name"]: f for f in r.json().get("fields", [])}

        add, replace = [], []
        for d in defs:
            if d["name"] not in existing:
                add.append(d)
            else:
                current = existing[d["name"]]
                if any(current.get(k) != v for k, v in d.items()):
                    replace.append(d)

        commands: dict = {}
        if add:
            commands["add-field"] = add
        if replace:
            commands["replace-field"] = replace
        if not commands:
            return "schema already matches the profile — nothing to do"

        r = client.post(schema_url, params={"wt": "json"}, json=commands)
        body = r.json()
        if r.status_code != 200 or body.get("errors"):
            sys.exit(f"searchlab: schema update failed: {json.dumps(body, indent=2)}")

    parts = []
    if add:
        parts.append(f"added {len(add)} field(s): {', '.join(d['name'] for d in add)}")
    if replace:
        parts.append(f"replaced {len(replace)} field(s): {', '.join(d['name'] for d in replace)}")
    return "\n".join(parts)
