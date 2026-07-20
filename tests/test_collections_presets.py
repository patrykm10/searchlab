"""Collection-management validation and document-shape presets."""

from __future__ import annotations

from searchlab.actions import DOC_PRESETS, FALLBACK_PROFILE, ActionRunner
from searchlab.cluster import ClusterSpec
from searchlab.datagen import generate, load_profile


# ------------------------------------------------------------- presets ---

def _profile_for(preset: str, tmp_path):
    text = DOC_PRESETS[preset] or FALLBACK_PROFILE
    p = tmp_path / f"{preset}.yaml"
    p.write_text(text)
    return load_profile(p)


def test_all_presets_parse_and_generate(tmp_path):
    for preset in DOC_PRESETS:
        profile = _profile_for(preset, tmp_path)
        docs = list(generate(profile, 50, seed=1))
        assert len(docs) == 50
        assert all("id" in d for d in docs)


def test_simple_preset_is_actually_small(tmp_path):
    docs = list(generate(_profile_for("simple", tmp_path), 100, seed=2))
    for d in docs:
        assert 20 <= len(d["body_t"].split()) <= 60
    assert len(docs[0]) == 5  # id + 4 fields, nothing more


def test_heavy_text_preset_is_actually_heavy(tmp_path):
    docs = list(generate(_profile_for("heavy-text", tmp_path), 30, seed=3))
    for d in docs:
        assert 300 <= len(d["body_t"].split()) <= 800


def test_high_cardinality_preset_has_many_distinct_values(tmp_path):
    docs = list(generate(_profile_for("high-cardinality", tmp_path), 500, seed=4))
    assert len({d["user_id_s"] for d in docs}) > 450   # effectively unique
    assert all(isinstance(d["tags_ss"], list) for d in docs)
    all_tags = {t for d in docs for t in d["tags_ss"]}
    assert len(all_tags) > 800  # drawn from a 5000-value space


# --------------------------------------------- runner validation (no HTTP) ---

def test_index_docs_rejects_unknown_preset():
    runner = ActionRunner(ClusterSpec())
    out = runner.index_docs("products", 1000, preset="artisanal")
    assert out["ok"] is False and "shape" in out["error"].lower()


def test_create_collection_validates_before_any_work():
    runner = ActionRunner(ClusterSpec())
    assert "names" in runner.create_collection("bad name!", 2, 1)["error"]
    assert "Shards" in runner.create_collection("ok", 99, 1)["error"]
    assert "Copies" in runner.create_collection("ok", 2, 99)["error"]
    runner._coll_busy = True
    assert "already running" in runner.create_collection("ok", 2, 1)["error"]


def test_delete_collection_guards():
    runner = ActionRunner(ClusterSpec())
    assert runner.delete_collection("")["ok"] is False
    runner._coll_busy = True
    assert "already running" in runner.delete_collection("products")["error"]
