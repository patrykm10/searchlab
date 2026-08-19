"""Scenario catalog: config resolution, engine parity, and drill compatibility.

The parity tests matter more than they look. The recurring failure in this
codebase is a feature written for Solr that silently does nothing on ES/OS —
not an error, just an empty result that reads as an idle cluster. A scenario
that ships only a Solr query file, or that names `solr2` in its chaos steps,
fails exactly that way. These tests make that shape unshippable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from searchlab import drill, loadtest
from searchlab import scenarios as scn
from searchlab.cluster import ClusterSpec

SHIPPED = [s["name"] for s in scn.catalog()]
ENGINES = ("solr", "elasticsearch", "opensearch")


def spec(engine="solr", **kw):
    return ClusterSpec(engine=engine, **kw)


# ------------------------------------------------------------------ catalog ---

def test_catalog_finds_the_shipped_scenarios():
    assert set(SHIPPED) >= {"facet-pressure", "merge-storm", "deep-paging", "node-loss"}


def test_catalog_entries_carry_a_title():
    for s in scn.catalog():
        assert s["error"] is None, f"{s['name']} failed to parse: {s['error']}"
        assert s["title"], f"{s['name']} has no title — it is what `list` prints"


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_scenario_validates(name):
    cfg = scn.load(name)
    assert cfg["name"] == name


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_scenario_explains_what_to_watch(name):
    """A scenario without `watch` is just a load test with a nickname."""
    assert scn.load(name)["watch"], f"{name} declares nothing to watch"


@pytest.mark.parametrize("name", SHIPPED)
def test_referenced_profile_exists(name):
    assert Path(scn.load(name)["data"]["profile"]).is_file()


# ------------------------------------------------------------------ parity ---

@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("engine", ENGINES)
def test_every_scenario_resolves_queries_on_every_engine(name, engine):
    """No engine may fall through to a query file that does not exist."""
    path = scn.queries_for(scn.load(name), engine)
    if path is not None:
        assert Path(path).is_file(), f"{name} on {engine} points at missing {path}"


@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("engine", ENGINES)
def test_referenced_query_files_are_loadable(name, engine):
    path = scn.queries_for(scn.load(name), engine)
    if path is None:
        return
    templates = loadtest.load_queries(path)
    assert templates, f"{path} has no templates"
    for t in templates:
        assert "params" in t or "body" in t, f"{path}: {t.get('name')} has neither params nor body"


@pytest.mark.parametrize("name", SHIPPED)
def test_solr_and_es_get_different_query_dialects(name):
    """Solr speaks params, ES/OS speak a JSON body. A scenario handing one
    engine the other's file produces a run of uniform errors, which reads as a
    broken cluster rather than a broken config."""
    solr_path = scn.queries_for(scn.load(name), "solr")
    es_path = scn.queries_for(scn.load(name), "opensearch")
    if solr_path is None or es_path is None:
        return
    assert all("params" in t for t in loadtest.load_queries(solr_path))
    assert all("body" in t for t in loadtest.load_queries(es_path))


@pytest.mark.parametrize("engine,expected", [
    ("solr", "solr2"), ("elasticsearch", "es2"), ("opensearch", "os2"),
])
def test_generic_node_names_resolve_per_engine(engine, expected):
    assert scn.resolve_node("node2", spec(engine)) == expected


def test_literal_node_names_pass_through():
    assert scn.resolve_node("solr1", spec("solr")) == "solr1"


def test_node_beyond_the_cluster_is_refused():
    with pytest.raises(SystemExit, match="node5"):
        scn.resolve_node("node5", spec("solr", solr_nodes=2))


def test_zookeeper_is_not_counted_as_a_data_node():
    """Solr's node_names appends zk1..N; node2 must still mean the second Solr."""
    assert scn.resolve_node("node2", spec("solr", solr_nodes=2, zk_nodes=3)) == "solr2"


@pytest.mark.parametrize("engine", ENGINES)
def test_chaos_steps_resolve_on_every_engine(engine):
    cfg = scn.load("node-loss")
    steps = scn.resolved_chaos(cfg, spec(engine, solr_nodes=2))
    assert steps and all(not s["node"].startswith("node") for s in steps)


# ----------------------------------------------------------- query dispatch ---

def test_queries_may_be_a_bare_path():
    assert scn.queries_for({"queries": "queries/default.yaml"}, "opensearch") == "queries/default.yaml"


def test_queries_absent_falls_back_to_engine_defaults():
    assert scn.queries_for({}, "solr") is None


def test_es_family_shares_one_key():
    cfg = {"queries": {"solr": "s.yaml", "es": "e.yaml"}}
    assert scn.queries_for(cfg, "elasticsearch") == "e.yaml"
    assert scn.queries_for(cfg, "opensearch") == "e.yaml"
    assert scn.queries_for(cfg, "solr") == "s.yaml"


def test_exact_engine_beats_family():
    cfg = {"queries": {"es": "family.yaml", "opensearch": "exact.yaml"}}
    assert scn.queries_for(cfg, "opensearch") == "exact.yaml"


def test_default_key_catches_the_rest():
    assert scn.queries_for({"queries": {"default": "d.yaml"}}, "solr") == "d.yaml"


def test_missing_engine_entry_is_refused_not_silently_skipped():
    with pytest.raises(SystemExit, match="solr"):
        scn.queries_for({"queries": {"es": "e.yaml"}}, "solr")


# -------------------------------------------------------------- validation ---

@pytest.mark.parametrize("missing", ["name", "title", "data", "load"])
def test_missing_top_level_section_is_refused(missing):
    cfg = {"name": "x", "title": "t", "data": {"collection": "c", "profile": "p"},
           "load": {"rps": 1, "duration": 1}}
    cfg.pop(missing)
    with pytest.raises(SystemExit, match=missing):
        scn.validate(cfg, "test")


def test_chaos_outside_the_load_window_is_refused():
    cfg = {"name": "x", "title": "t", "data": {"collection": "c", "profile": "p"},
           "load": {"rps": 1, "duration": 60},
           "chaos": [{"at": 90, "action": "kill", "node": "node1"}]}
    with pytest.raises(SystemExit, match="outside"):
        scn.validate(cfg, "test")


def test_unknown_scenario_name_lists_what_exists():
    with pytest.raises(SystemExit, match="facet-pressure"):
        scn.load("no-such-scenario")


# ------------------------------------------------------------- drill bridge ---

@pytest.mark.parametrize("name", SHIPPED)
def test_to_drill_cfg_round_trips_through_the_drill_loader(name, tmp_path):
    """The scenario's whole execution story is `drill.run_drill`. If drill's own
    validator rejects what a scenario produces, the scenario cannot run."""
    cfg = scn.to_drill_cfg(scn.load(name), spec("solr", solr_nodes=2))
    path = tmp_path / "d.yaml"
    path.write_text(yaml.safe_dump(cfg))
    loaded = drill.load_drill(path)
    assert loaded["collection"] == cfg["collection"]
    assert loaded["load"]["rps"] == cfg["load"]["rps"]


def test_to_drill_cfg_carries_the_engine_specific_query_file():
    cfg = scn.load("facet-pressure")
    assert scn.to_drill_cfg(cfg, spec("solr"))["load"]["queries"].endswith("facet-pressure.yaml")
    assert scn.to_drill_cfg(cfg, spec("opensearch"))["load"]["queries"].endswith("facet-pressure-es.yaml")


# ---------------------------------------------------------------- warnings ---

def test_heap_mismatch_is_reported():
    cfg = scn.load("facet-pressure")
    assert any("heap" in w for w in scn.cluster_warnings(cfg, spec("solr", heap="4g")))


def test_matching_cluster_produces_no_noise():
    cfg = {"cluster": {"heap": "1g", "nodes": 2, "engine": "solr"}}
    assert scn.cluster_warnings(cfg, spec("solr", heap="1g", solr_nodes=2)) == []


def test_engine_any_never_warns():
    assert scn.cluster_warnings({"cluster": {"engine": "any"}}, spec("opensearch")) == []


def test_solr_opts_warning_does_not_quote_solr_flags_at_opensearch():
    """Repeating -Dsolr... at an OpenSearch cluster is the cross-engine
    nonsense the rest of the UI works hard to avoid."""
    warnings = scn.cluster_warnings(
        {"cluster": {"solr_opts": "-Dsolr.autoSoftCommit.maxTime=500"}}, spec("opensearch"))
    assert warnings and "-Dsolr" not in warnings[0]
    assert "refresh_interval" in warnings[0]


# -------------------------------------------------------------------- plan ---

@pytest.mark.parametrize("name", SHIPPED)
@pytest.mark.parametrize("engine", ENGINES)
def test_plan_is_complete_on_every_engine(name, engine):
    p = scn.plan(scn.load(name), spec(engine, solr_nodes=2))
    for key in ("name", "title", "collection", "profile", "count", "queries",
                "rps", "duration", "chaos", "warnings", "watch", "report"):
        assert key in p
    assert isinstance(p["count"], int) and p["count"] > 0


# ------------------------------------------------------- checkout-relative ---

@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_scenarios_have_all_their_files_in_the_checkout(name):
    assert scn.missing_files(scn.load(name), spec("solr")) == []


def test_missing_files_names_every_gap_not_just_the_first():
    cfg = {"data": {"collection": "c", "profile": "nope/profile.yaml"},
           "queries": "nope/queries.yaml"}
    assert scn.missing_files(cfg, spec("solr")) == ["nope/profile.yaml", "nope/queries.yaml"]


def test_scenario_dir_can_be_overridden(tmp_path, monkeypatch):
    """A team's own scenarios are a directory of text files."""
    (tmp_path / "mine.yaml").write_text(
        "name: mine\ntitle: My scenario\n"
        "data: {collection: c, profile: profiles/default.yaml}\n"
        "load: {rps: 1, duration: 10}\nwatch: [something]\n")
    monkeypatch.setenv("SEARCHLAB_SCENARIOS", str(tmp_path))
    assert "mine" in [s["name"] for s in scn.catalog()]
    assert scn.load("mine")["title"] == "My scenario"


def test_own_scenarios_shadow_the_shipped_ones(tmp_path, monkeypatch):
    (tmp_path / "deep-paging.yaml").write_text(
        "name: deep-paging\ntitle: Mine instead\n"
        "data: {collection: c, profile: profiles/default.yaml}\n"
        "load: {rps: 1, duration: 10}\n")
    monkeypatch.setenv("SEARCHLAB_SCENARIOS", str(tmp_path))
    assert scn.load("deep-paging")["title"] == "Mine instead"


# ------------------------------------------------ malformed but valid YAML ---

@pytest.mark.parametrize("section", ["data", "load", "name", "title"])
def test_empty_section_gets_the_clean_message_not_a_typeerror(section):
    """`data:` with nothing indented under it is valid YAML yielding None.
    Presence is not enough — every other path here exits with a sentence, and
    this one used to raise TypeError from inside the membership test."""
    cfg = {"name": "x", "title": "t", "data": {"collection": "c", "profile": "p"},
           "load": {"rps": 1, "duration": 1}}
    cfg[section] = None
    with pytest.raises(SystemExit, match=section):
        scn.validate(cfg, "test")


@pytest.mark.parametrize("section", ["data", "load"])
def test_scalar_where_a_mapping_belongs_is_refused(section):
    cfg = {"name": "x", "title": "t", "data": {"collection": "c", "profile": "p"},
           "load": {"rps": 1, "duration": 1}}
    cfg[section] = "oops"
    with pytest.raises(SystemExit, match="must be a mapping"):
        scn.validate(cfg, "test")


def test_chaos_must_be_a_list():
    cfg = {"name": "x", "title": "t", "data": {"collection": "c", "profile": "p"},
           "load": {"rps": 1, "duration": 1}, "chaos": {"at": 1}}
    with pytest.raises(SystemExit, match="list of steps"):
        scn.validate(cfg, "test")


def test_one_unreadable_file_does_not_take_down_the_listing(tmp_path, monkeypatch):
    """catalog() promises a broken scenario the user can see over one that
    silently is not there. It caught YAMLError only, so a file that is not
    valid UTF-8 raised out of the loop and killed the whole listing."""
    (tmp_path / "fine.yaml").write_text(
        "name: fine\ntitle: Fine\ndata: {collection: c, profile: p}\n"
        "load: {rps: 1, duration: 1}\n")
    (tmp_path / "binary.yaml").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    monkeypatch.setenv("SEARCHLAB_SCENARIOS", str(tmp_path))

    listed = {s["name"]: s for s in scn.catalog()}
    assert "fine" in listed and listed["fine"]["error"] is None
    assert "binary" in listed and listed["binary"]["error"]


def test_unparseable_yaml_is_listed_with_its_error(tmp_path, monkeypatch):
    (tmp_path / "broken.yaml").write_text("name: broken\n  bad: [indent\n")
    monkeypatch.setenv("SEARCHLAB_SCENARIOS", str(tmp_path))
    entry = next(s for s in scn.catalog() if s["name"] == "broken")
    assert entry["error"]


@pytest.mark.parametrize("step", [
    "at action node",   # contains all three keys AS SUBSTRINGS: the crash case
    "pause node2",      # contains none: exited cleanly even before the fix
    ["at", "action"],
    42,
])
def test_chaos_step_that_is_not_a_mapping_is_refused(step):
    """`"at" not in step` is a substring test on a string, so a step reading
    "at action node" satisfied all three key guards and crashed on step["at"].
    The plausible wording was the one that broke; the implausible one did not."""
    cfg = {"name": "x", "title": "t", "data": {"collection": "c", "profile": "p"},
           "load": {"rps": 1, "duration": 60}, "chaos": [step]}
    with pytest.raises(SystemExit, match="must be a mapping|needs at/action/node"):
        scn.validate(cfg, "test")
