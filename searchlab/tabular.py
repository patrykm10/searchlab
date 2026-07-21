"""Read real documents from CSV/TSV/JSON files, not just generated ones.

Synthetic profiles approximate the *shape* of data; real files are the
shape. The friction is that the lab's collections rely on Solr's
`_default` dynamic fields, which key off name suffixes — a column called
`price` isn't a field Solr knows what to do with, but `price_f` is. So
this module infers a type per column from a sample of rows and renames
columns onto the matching suffix, leaving names that already carry a
valid suffix untouched.

Everything is reported rather than assumed: `describe()` returns the
mapping so `index --dry-run` can show exactly what will happen before a
single document is sent.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

# Suffix -> Solr dynamic field type, per the _default managed schema.
SUFFIXES = {
    "_i": "int", "_l": "long", "_f": "float", "_d": "double",
    "_b": "boolean", "_dt": "date", "_s": "string", "_t": "text",
    "_ss": "string (multi)", "_is": "int (multi)", "_txt": "text",
}
_TYPE_SUFFIX = {
    "int": "_i", "long": "_l", "float": "_f", "double": "_d",
    "boolean": "_b", "date": "_dt", "string": "_s", "text": "_t",
}

_BOOLS = {"true": True, "false": False, "yes": True, "no": False,
          "t": True, "f": False}
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
)
_SAMPLE_ROWS = 200          # rows read to infer types
_TEXT_WORDS = 4             # above this median word count, treat as analyzed text


def sniff_format(path: Path) -> str:
    """One of jsonl | json | csv | tsv, from the extension then the content."""
    ext = path.suffix.lower()
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    if ext == ".json":
        return "json"
    if ext == ".tsv":
        return "tsv"
    if ext == ".csv":
        return "csv"
    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.readline()
    stripped = head.lstrip()
    if stripped.startswith("["):
        return "json"
    if stripped.startswith("{"):
        return "jsonl"
    return "tsv" if head.count("\t") > head.count(",") else "csv"


def _parse_date(value: str) -> str | None:
    """Normalize to the Zulu format Solr's date field wants."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def _looks_like(values: list[str]) -> str:
    """Infer a Solr type from sample values, narrowest that fits them all."""
    if not values:
        return "string"
    if all(v.lower() in _BOOLS for v in values):
        return "boolean"
    try:
        ints = [int(v) for v in values]
    except ValueError:
        pass
    else:
        return "long" if any(abs(i) > 2_147_483_647 for i in ints) else "int"
    try:
        for v in values:
            float(v)
    except ValueError:
        pass
    else:
        return "float"
    if all(_parse_date(v) for v in values):
        return "date"
    words = sorted(len(v.split()) for v in values)
    median = words[len(words) // 2]
    return "text" if median > _TEXT_WORDS else "string"


def _has_known_suffix(name: str) -> bool:
    return any(name.endswith(s) for s in SUFFIXES)


def _target_name(column: str, solr_type: str) -> str:
    """Column renamed onto a dynamic-field suffix; `id` and already-suffixed
    names are left alone."""
    if column == "id" or _has_known_suffix(column):
        return column
    safe = re.sub(r"\W+", "_", column.strip()).strip("_") or "field"
    return safe + _TYPE_SUFFIX.get(solr_type, "_s")


def _rows_from_file(path: Path) -> Iterator[dict]:
    fmt = sniff_format(path)
    if fmt == "json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):          # {"docs": [...]} or a single doc
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        yield from data
        return
    if fmt == "jsonl":
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)
        return
    delimiter = "\t" if fmt == "tsv" else ","
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f, delimiter=delimiter):
            yield {k: v for k, v in row.items() if k}


def describe(path: Path) -> dict:
    """Inferred column mapping, without indexing anything.

    {"format", "columns": [{"column", "type", "field", "sample"}],
     "generated_id": bool}
    """
    fmt = sniff_format(path)
    sample: list[dict] = []
    for row in _rows_from_file(path):
        sample.append(row)
        if len(sample) >= _SAMPLE_ROWS:
            break

    columns: list[dict] = []
    seen: list[str] = []
    for row in sample:
        for key in row:
            if key not in seen:
                seen.append(key)
    for column in seen:
        raw = [str(r[column]) for r in sample
               if r.get(column) not in (None, "")]
        solr_type = "string" if fmt in ("json", "jsonl") and not raw \
            else _looks_like(raw)
        columns.append({
            "column": column,
            "type": solr_type,
            "field": _target_name(column, solr_type),
            "sample": raw[0][:60] if raw else "",
        })
    return {"format": fmt, "columns": columns,
            "generated_id": not any(c["field"] == "id" for c in columns),
            "sampled": len(sample)}


def _coerce(value, solr_type: str):
    if value is None or value == "":
        return None
    text = str(value)
    if solr_type == "boolean":
        return _BOOLS.get(text.lower(), None)
    if solr_type in ("int", "long"):
        try:
            return int(text)
        except ValueError:
            return None
    if solr_type in ("float", "double"):
        try:
            return float(text)
        except ValueError:
            return None
    if solr_type == "date":
        return _parse_date(text)
    return value


def read_documents(path: Path) -> Iterator[dict]:
    """Documents ready to index: columns renamed onto dynamic fields, values
    coerced to the inferred type, and an `id` synthesized when absent."""
    plan = describe(path)
    mapping = {c["column"]: (c["field"], c["type"]) for c in plan["columns"]}
    needs_id = plan["generated_id"]
    stem = path.stem
    for n, row in enumerate(_rows_from_file(path)):
        doc: dict = {}
        for column, value in row.items():
            target = mapping.get(column)
            if target is None:                      # column absent from sample
                target = (_target_name(column, "string"), "string")
            field, solr_type = target
            coerced = _coerce(value, solr_type)
            if coerced is not None:
                doc[field] = coerced
        if needs_id:
            doc["id"] = f"{stem}-{n}"
        if doc:
            yield doc
