"""Reading real documents: format sniffing, type inference, field mapping."""

from __future__ import annotations

import json

import pytest

from searchlab.indexer import _read_batches
from searchlab.tabular import describe, read_documents, sniff_format

CATALOG = (
    "sku,product name,description,price,stock,in_stock,released\n"
    "A-1001,Wireless Mouse,A compact wireless mouse with silent clicks and long life,29.99,412,true,2024-03-15\n"
    "A-1002,Mechanical Keyboard,Tactile keyboard with hot swappable switches and backlight,119.00,87,true,2023-11-02\n"
    "A-1003,4K Monitor,Twenty seven inch IPS display with USB-C power delivery,399.50,0,false,2024-01-20\n"
)


@pytest.fixture
def catalog(tmp_path):
    p = tmp_path / "catalog.csv"
    p.write_text(CATALOG)
    return p


def _fields(plan):
    return {c["column"]: (c["field"], c["type"]) for c in plan["columns"]}


def test_sniff_formats(tmp_path):
    csvp = tmp_path / "a.csv"; csvp.write_text("a,b\n1,2\n")
    tsvp = tmp_path / "a.tsv"; tsvp.write_text("a\tb\n1\t2\n")
    jsonl = tmp_path / "a.jsonl"; jsonl.write_text('{"a":1}\n')
    jsn = tmp_path / "a.json"; jsn.write_text('[{"a":1}]')
    assert sniff_format(csvp) == "csv"
    assert sniff_format(tsvp) == "tsv"
    assert sniff_format(jsonl) == "jsonl"
    assert sniff_format(jsn) == "json"
    # extensionless files fall back to content
    plain = tmp_path / "data"; plain.write_text("a\tb\tc\n1\t2\t3\n")
    assert sniff_format(plain) == "tsv"


def test_infers_types_and_maps_to_dynamic_fields(catalog):
    f = _fields(describe(catalog))
    assert f["price"] == ("price_f", "float")
    assert f["stock"] == ("stock_i", "int")
    assert f["in_stock"] == ("in_stock_b", "boolean")
    assert f["released"] == ("released_dt", "date")
    assert f["sku"] == ("sku_s", "string")            # short -> exact-match string
    assert f["description"][1] == "text"              # long -> analyzed text
    assert f["description"][0] == "description_t"
    assert f["product name"][0] == "product_name_s"   # spaces sanitized


def test_values_are_coerced_not_left_as_strings(catalog):
    doc = next(iter(read_documents(catalog)))
    assert doc["price_f"] == 29.99 and isinstance(doc["price_f"], float)
    assert doc["stock_i"] == 412 and isinstance(doc["stock_i"], int)
    assert doc["in_stock_b"] is True
    assert doc["released_dt"] == "2024-03-15T00:00:00Z"   # Solr's Zulu format


def test_generates_ids_when_absent(catalog):
    docs = list(read_documents(catalog))
    assert [d["id"] for d in docs] == ["catalog-0", "catalog-1", "catalog-2"]


def test_existing_id_and_suffixed_columns_left_alone(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("id,title_t,price_f\nx1,already suffixed,3.5\n")
    plan = describe(p)
    assert plan["generated_id"] is False
    f = _fields(plan)
    assert f["id"][0] == "id"
    assert f["title_t"][0] == "title_t"     # not re-suffixed to title_t_s
    assert f["price_f"][0] == "price_f"
    doc = next(iter(read_documents(p)))
    assert doc["id"] == "x1"


def test_blank_cells_are_omitted_not_indexed_as_empty(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("id,note,qty\n1,,5\n2,hello there friend indeed,\n")
    docs = list(read_documents(p))
    assert "note" not in docs[0] and "note_s" not in docs[0]
    assert "qty_i" not in docs[1]


def test_tsv_and_json_array_round_trip(tmp_path):
    tsv = tmp_path / "d.tsv"
    tsv.write_text("id\tqty\nx\t7\n")
    assert next(iter(read_documents(tsv)))["qty_i"] == 7

    jsn = tmp_path / "d.json"
    jsn.write_text(json.dumps([{"id": "a", "n": 2}, {"id": "b", "n": 3}]))
    assert [d["id"] for d in read_documents(jsn)] == ["a", "b"]


def test_indexer_batches_csv_and_passes_jsonl_through(tmp_path, catalog):
    batches = list(_read_batches(catalog, batch_size=2))
    assert [len(b) for b in batches] == [2, 1]
    assert batches[0][0]["price_f"] == 29.99      # went through the mapper

    raw = tmp_path / "raw.jsonl"
    raw.write_text('{"id":"a","untouched":1}\n{"id":"b","untouched":2}\n')
    jl = list(_read_batches(raw, batch_size=10))
    assert jl[0][0] == {"id": "a", "untouched": 1}   # JSONL unchanged


def test_large_integers_become_long(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("id,big\n1,9999999999\n")
    assert _fields(describe(p))["big"] == ("big_l", "long")
