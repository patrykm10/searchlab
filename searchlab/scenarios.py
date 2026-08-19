"""Named reproductions: the repro recipes, as things the tool actually does.

The README has always described how to reproduce a facet-pressure heap problem
or a merge storm. The description was six commands ending in "now facet on
user_id_s and watch the GC logs" — which is homework, with the interesting part
left as an exercise. A scenario is that recipe as an object: the data shape, the
query mix, the cluster it wants, the faults to inject, and — the part that makes
it a lesson rather than a run — what to look at while it happens.

    searchlab scenario list
    searchlab scenario show facet-pressure
    searchlab scenario run facet-pressure

Everything below is configuration handling: parsing, validating, resolving
against whatever cluster is actually running, and translating into the drill
config that `drill.run_drill` already knows how to execute. No new execution
machinery — a scenario is a drill someone else already worked out for you.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from . import gates
from .engines import get_engine

_REQUIRED = ("name", "title", "data", "load")
_DATA_REQUIRED = ("collection", "profile")

# Query files are picked per engine family: Solr's params and the ES/OS query
# DSL are different languages, and a scenario that ships only one of them is
# the silent-no-op-on-the-other-engine bug this codebase keeps rediscovering.
_FAMILY = {"solr": "solr", "elasticsearch": "es", "opensearch": "es"}


def _chaos_actions() -> dict:
    """The one real registry of fault actions, imported late to keep this
    module importable without the docker-facing code."""
    from . import chaos
    return chaos._ACTIONS


def catalog_dirs() -> list[Path]:
    """Where scenarios are looked for: your own directory first, then the ones
    in the checkout.

    Scenarios reference `profiles/` and `queries/` by repo-relative path, the
    same way the README's commands do, so they belong to the checkout rather
    than the wheel. $SEARCHLAB_SCENARIOS points at your own set — a team's
    onboarding scenarios are a directory of text files.
    """
    dirs = []
    if os.environ.get("SEARCHLAB_SCENARIOS"):
        dirs.append(Path(os.environ["SEARCHLAB_SCENARIOS"]))
    dirs.append(Path("scenarios"))
    return dirs


def catalog() -> list[dict]:
    """Every scenario that parses, sorted by name. A file that fails to parse
    is listed with its error rather than skipped — a broken scenario the user
    can see beats one that silently is not there."""
    found: dict[str, dict] = {}
    for d in catalog_dirs():
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.yaml")):
            if path.stem in found:
                continue  # first directory wins
            try:
                cfg = yaml.safe_load(path.read_text()) or {}
                found[path.stem] = {
                    "name": cfg.get("name", path.stem),
                    "title": cfg.get("title", ""),
                    "about": cfg.get("about", ""),
                    "path": path,
                    "error": None,
                }
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                found[path.stem] = {"name": path.stem, "title": "", "about": "",
                                    "path": path, "error": str(e)}
    return [found[k] for k in sorted(found)]


def find(name: str) -> Path:
    """A scenario name, or a path to a YAML file. Names win over paths, so a
    typo'd name reports the catalog rather than a file-not-found."""
    for d in catalog_dirs():
        candidate = d / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    as_path = Path(name)
    if as_path.is_file():
        return as_path
    known = ", ".join(s["name"] for s in catalog()) or "none found"
    sys.exit(f"searchlab: no scenario '{name}' — available: {known}")


def load(name: str) -> dict:
    path = find(name)
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        sys.exit(f"searchlab: scenario {path} is not valid YAML — {e}")
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(f"searchlab: scenario {path} could not be read — {e}")
    return validate(cfg, path)


def validate(cfg: dict, source: str | Path) -> dict:
    if not isinstance(cfg, dict):
        sys.exit(f"searchlab: scenario {source} must be a YAML mapping")
    for key in _REQUIRED:
        if cfg.get(key) is None:
            # `data:` with nothing indented under it is valid YAML yielding
            # None, so presence is not enough — ask for the value.
            sys.exit(f"searchlab: scenario {source} needs a '{key}' section")
    for key in ("data", "load"):
        if not isinstance(cfg[key], dict):
            sys.exit(f"searchlab: scenario {source} '{key}' must be a mapping, "
                     f"got {type(cfg[key]).__name__}")
    for key in _DATA_REQUIRED:
        if cfg["data"].get(key) is None:
            sys.exit(f"searchlab: scenario {source} data section needs '{key}'")
    load_cfg = cfg["load"]
    for key in ("rps", "duration"):
        # `duration:` with no value is the same trap as an empty section, one
        # level down: present, None, and TypeError at the first float().
        if load_cfg.get(key) is None:
            sys.exit(f"searchlab: scenario {source} load section needs '{key}'")
    # `2m` and `1h30m` work everywhere else in this tool; parse them the same
    # way here rather than making scenarios the one place that wants seconds.
    duration = gates.parse_duration(load_cfg["duration"])
    try:
        float(load_cfg["rps"])
    except (TypeError, ValueError):
        sys.exit(f"searchlab: scenario {source} load rps must be a number, "
                 f"got {load_cfg['rps']!r}")
    if not isinstance(cfg.get("chaos") or [], list):
        sys.exit(f"searchlab: scenario {source} 'chaos' must be a list of steps")
    for step in cfg.get("chaos") or []:
        # `"at" not in step` is a SUBSTRING test when step is a string, not a
        # key test — so the plausible-looking `- "at action node"` satisfied all
        # three guards and then crashed on step["at"], while `- "pause node2"`
        # exited cleanly. Same authoring mistake, opposite outcomes.
        if not isinstance(step, dict):
            sys.exit(f"searchlab: scenario {source} chaos step {step!r} must be "
                     f"a mapping with at/action/node")
        if "at" not in step or "action" not in step or "node" not in step:
            sys.exit(f"searchlab: scenario {source} chaos step {step} needs at/action/node")
        # Having an `action` is not the same as having a real one. A scenario
        # goes to run_drill() through to_drill_cfg(), so load_drill()'s own
        # check never runs on this path — without this, `unpuase` costs a
        # collection, 150k indexed documents and the start of a load test
        # before failing as a bare KeyError from inside the chaos thread.
        if step["action"] not in _chaos_actions():
            sys.exit(f"searchlab: scenario {source} has unknown chaos action "
                     f"'{step['action']}' — valid: {', '.join(sorted(_chaos_actions()))}")
        if gates.parse_duration(step["at"]) >= duration:
            sys.exit(f"searchlab: scenario {source} chaos step at t={step['at']} is "
                     f"outside the {load_cfg['duration']} load window")
    cfg.setdefault("watch", [])
    cfg.setdefault("chaos", [])
    return cfg


def queries_for(cfg: dict, engine: str) -> str | None:
    """The query file for this engine, or None to fall back to engine defaults.

    Accepts a bare path (all engines) or a mapping keyed by engine name, by
    family (`solr` / `es`), or `default`.
    """
    q = cfg.get("queries")
    if q is None or isinstance(q, str):
        return q
    if not isinstance(q, dict):
        sys.exit("searchlab: scenario 'queries' must be a path or a mapping of engine -> path")
    engine = get_engine(engine).name
    for key in (engine, _FAMILY.get(engine, engine), "default"):
        if key in q:
            return q[key]
    sys.exit(f"searchlab: scenario has no query file for engine '{engine}' — "
             f"it declares: {', '.join(sorted(q))}")


def resolve_node(node: str, spec) -> str:
    """`node2` -> whatever the running engine calls its second node.

    Nodes are solr2 on Solr, es2 on Elasticsearch, os2 on OpenSearch. A
    scenario written against one engine's names does nothing on the others —
    it fails looking for a container that was never going to exist — so
    scenarios say `nodeN` and the name is resolved here, against the cluster
    that is actually up. Literal names still pass through untouched.
    """
    node = str(node)
    if not (node.startswith("node") and node[4:].isdigit()):
        return node
    index = int(node[4:])
    names = get_engine(spec.engine).node_names(spec)
    data_nodes = [n for n in names if not n.startswith("zk")]
    if not 1 <= index <= len(data_nodes):
        sys.exit(f"searchlab: scenario wants {node} but the cluster has "
                 f"{len(data_nodes)} node(s): {', '.join(data_nodes)}")
    return data_nodes[index - 1]


def resolved_chaos(cfg: dict, spec) -> list[dict]:
    return [{**step, "node": resolve_node(step["node"], spec)}
            for step in cfg.get("chaos", [])]


def cluster_warnings(cfg: dict, spec) -> list[str]:
    """What differs between the cluster this scenario wants and the one that is
    running. Advisory on purpose: re-provisioning costs minutes, and a scenario
    that reproduces weakly is still worth watching — as long as you were told.
    """
    want = cfg.get("cluster") or {}
    out = []
    if "engine" in want and want["engine"] not in ("any", spec.engine):
        out.append(f"wants engine {want['engine']}, running {spec.engine}")
    if "heap" in want and want["heap"] != spec.heap:
        out.append(f"wants heap {want['heap']}, running {spec.heap} — "
                   f"a larger heap absorbs the pressure and may hide the effect")
    if "nodes" in want and int(want["nodes"]) != int(spec.solr_nodes):
        out.append(f"wants {want['nodes']} node(s), running {spec.solr_nodes}")
    # solr_opts is a Solr-only concept; repeating it at an OpenSearch cluster
    # would be exactly the cross-engine nonsense the UI is supposed to avoid.
    # Killing a node in a collection with one copy of each shard does not
    # demonstrate failover, it demonstrates data loss. Both engines take
    # `replicas` as copies-per-shard here (Solr replicationFactor; ES/OS
    # number_of_replicas + 1), so 1 means no redundancy on either.
    if cfg.get("chaos") and int((cfg.get("data") or {}).get("replicas", 1)) < 2:
        out.append("this scenario injects faults but asks for 1 copy of each "
                   "shard — there is nothing to fail over to, so a killed node "
                   "loses data rather than shedding load")
    if "solr_opts" in want and spec.engine == "solr":
        if want["solr_opts"] not in (spec.solr_opts or ""):
            out.append(f"wants {want['solr_opts']!r}, which the running cluster "
                       f"does not set — the effect will be weaker")
    elif "solr_opts" in want:
        out.append("the commit-interval settings this wants are Solr flags; on "
                   "ES/OS the equivalent is index.refresh_interval, set per index")
    return out


def to_drill_cfg(cfg: dict, spec) -> dict:
    """A scenario is a drill with the thinking already done."""
    load_cfg = dict(cfg["load"])
    queries = queries_for(cfg, spec.engine)
    if queries:
        load_cfg["queries"] = queries
    return {
        "collection": cfg["data"]["collection"],
        "seed": cfg["data"].get("seed"),
        "load": load_cfg,
        "chaos": resolved_chaos(cfg, spec),
        "report": cfg.get("report", cfg["name"]),
        "assert": cfg.get("assert", []),
    }


def missing_files(cfg: dict, spec) -> list[str]:
    """Referenced files that are not there.

    Scenarios point at `profiles/` and `queries/` relative to the checkout, so
    running one from elsewhere fails on the first file it needs. Naming all of
    them up front beats a traceback from inside the generator on the file that
    happened to be needed first.
    """
    referenced = [cfg["data"]["profile"]]
    q = queries_for(cfg, spec.engine)
    if q:
        referenced.append(q)
    # index_profile drives the concurrent write stream. Missing it fails only
    # once the load starts, i.e. after the whole corpus has been indexed.
    index_profile = (cfg.get("load") or {}).get("index_profile")
    if index_profile:
        referenced.append(index_profile)
    return [f for f in referenced if not Path(f).is_file()]


def plan(cfg: dict, spec) -> dict:
    """Everything `run` would do, without doing any of it."""
    data = cfg["data"]
    load_cfg = cfg["load"]
    return {
        "name": cfg["name"],
        "title": cfg["title"],
        "engine": spec.engine,
        "collection": data["collection"],
        "profile": data["profile"],
        "count": int(data.get("count", 10_000)),
        "replicas": int(data.get("replicas", 1)),
        "seed": data.get("seed"),
        "queries": queries_for(cfg, spec.engine) or "(engine defaults)",
        "rps": load_cfg["rps"],
        "duration": load_cfg["duration"],
        "index_rps": load_cfg.get("index_rps", 0),
        "chaos": resolved_chaos(cfg, spec),
        "warnings": cluster_warnings(cfg, spec),
        "missing": missing_files(cfg, spec),
        "watch": cfg.get("watch", []),
        "report": cfg.get("report", cfg["name"]),
    }
