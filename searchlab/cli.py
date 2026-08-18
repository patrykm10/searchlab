"""searchlab CLI — disposable SolrCloud clusters, synthetic data, open-loop load tests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import click

from . import chaos as ch
from . import cluster as cl
from . import dashboard as db
from . import datagen, gates, indexer, loadtest, metrics
from . import drill as dr
from . import explain as ex
from . import gclog as gcl
from . import k8s as k8
from . import learn as ln
from . import recall as rc
from . import replay as rpl
from . import report as rp
from . import schema as sc
from . import scenarios as scn
from . import sweep as sw


@click.group()
def main() -> None:
    """Spin up SolrCloud, generate data, index it, and hammer it with load.

    Built for learning Solr internals and reproducing performance issues.
    """


# ---------------------------------------------------------------- cluster ---

@main.command()
@click.option("--engine", default="solr", show_default=True,
              type=click.Choice(["solr", "elasticsearch", "es", "opensearch", "os"]),
              help="Search engine to run.")
@click.option("--solr-version", "--version", "solr_version", default=None,
              help="Engine Docker image tag (default: solr 9.6, elasticsearch 8.14.3, opensearch 2.15.0).")
@click.option("--nodes", default=2, show_default=True, help="Number of Solr nodes.")
@click.option("--zk-nodes", default=1, show_default=True, help="Number of ZooKeeper nodes (1 or 3).")
@click.option("--heap", default="1g", show_default=True, help="SOLR_HEAP per node, e.g. 512m, 2g.")
@click.option("--gc-tune", default="", help='JVM GC flags via GC_TUNE, e.g. "-XX:+UseG1GC -XX:MaxGCPauseMillis=100".')
@click.option("--solr-opts", default="", help="Extra SOLR_OPTS, e.g. -Dsolr.autoSoftCommit.maxTime=1000.")
@click.option("--base-port", default=8983, show_default=True, help="Host port for solr1; node N gets base+N-1.")
@click.option("--monitoring/--no-monitoring", default=False, help="Add solr-exporter + Prometheus (:9090) + Grafana (:3000).")
@click.option("--gc-logs/--no-gc-logs", default=False, help="Mount ./.searchlab/gc-logs/<node> for GC log analysis.")
@click.option("--wait/--no-wait", default=True, help="Wait for all nodes to answer pings.")
def up(engine, solr_version, nodes, zk_nodes, heap, gc_tune, solr_opts, base_port, monitoring, gc_logs, wait):
    """Start a search cluster (SolrCloud, Elasticsearch, or OpenSearch) with Docker Compose."""
    from .engines import get_engine
    eng = get_engine(engine)
    defaults = {"solr": "9.6", "elasticsearch": "8.14.3", "opensearch": "2.15.0"}
    version = solr_version or defaults[eng.name]
    if eng.name != "solr":
        if base_port == 8983:
            base_port = eng.default_port
        if monitoring:
            raise SystemExit("searchlab: --monitoring is Solr-only for now")
        heap = heap.replace("gb", "g").replace("mb", "m")
    spec = cl.ClusterSpec(
        engine=eng.name, solr_version=version, solr_nodes=nodes, zk_nodes=zk_nodes,
        heap=heap, gc_tune=gc_tune, solr_opts=solr_opts, base_port=base_port,
        monitoring=monitoring, gc_logs=gc_logs,
    )
    cl.up(spec, wait=wait)
    ui = f"http://localhost:{base_port}/solr" if eng.name == "solr" else f"http://localhost:{base_port}"
    click.echo(f"\nCluster up ({eng.name} {version}). API: {ui}")
    if monitoring:
        click.echo("Prometheus: http://localhost:9090  Grafana: http://localhost:3000")


@main.command()
@click.option("--volumes", is_flag=True, help="Also remove volumes (data is lost).")
def down(volumes):
    """Stop and remove the cluster."""
    cl.down(volumes=volumes)


@main.command()
def status():
    """Show cluster status: live nodes and collection/index health."""
    spec = cl.load_spec()
    ov = cl.cluster_overview(spec)
    click.echo(f"engine: {spec.engine} {spec.solr_version}")
    click.echo(f"live nodes: {ov['live_nodes']} / {spec.solr_nodes}")
    click.echo(f"collections: {len(ov['collections'])}")
    for name, c in ov["collections"].items():
        click.echo(f"  {name}: {c['shards']} shard(s), health={c['health']}")


@main.command("create-collection")
@click.argument("name")
@click.option("--shards", default=1, show_default=True)
@click.option("--replicas", default=1, show_default=True)
@click.option("--config-set", default="_default", show_default=True)
def create_collection(name, shards, replicas, config_set):
    """Create a collection on the running cluster."""
    spec = cl.load_spec()
    cl.create_collection(spec, name, shards, replicas, config_set)
    click.echo(f"collection '{name}' created ({shards} shard(s) x {replicas} replica(s))")


@main.command("delete-collection")
@click.argument("name")
def delete_collection(name):
    """Delete a collection."""
    spec = cl.load_spec()
    cl.delete_collection(spec, name)
    click.echo(f"collection '{name}' deleted")


# ------------------------------------------------------------------- data ---

@main.command()
@click.option("--profile", "profile_path", required=True, type=click.Path(exists=True), help="YAML data profile.")
@click.option("--count", default="10000", show_default=True, help="Docs to generate (10000, 10k, 1.5m).")
@click.option("--out", default="data.jsonl", show_default=True)
@click.option("--seed", default=None, type=int, help="Seed for reproducible datasets.")
def gen(profile_path, count, out, seed):
    """Generate synthetic documents (JSONL) from a profile."""
    n = datagen.generate_to_file(profile_path, gates.parse_count(count), out, seed)
    click.echo(f"wrote {n} docs to {out}")


@main.command()
@click.option("--collection", required=True)
@click.option("--file", "path", required=True, type=click.Path(exists=True),
              help="JSONL, CSV, TSV, or JSON file to index.")
@click.option("--threads", default=4, show_default=True, help="Concurrent index workers.")
@click.option("--batch", default=500, show_default=True, help="Docs per update request.")
@click.option("--commit-within", default=10_000, show_default=True, help="commitWithin ms.")
@click.option("--dry-run", is_flag=True,
              help="Show how columns map onto Solr fields, index nothing.")
def index(collection, path, threads, batch, commit_within, dry_run):
    """Bulk-index a file into a collection.

    JSONL is indexed as-is. CSV/TSV/JSON columns are typed from a sample
    and renamed onto Solr's dynamic fields (price -> price_f); use
    --dry-run to see the mapping first.
    """
    if dry_run:
        from .tabular import describe, read_documents

        plan = describe(Path(path))
        click.echo(f"format: {plan['format']}  (sampled {plan['sampled']} rows)")
        width = max((len(c["column"]) for c in plan["columns"]), default=6)
        for c in plan["columns"]:
            arrow = "=" if c["field"] == c["column"] else "->"
            click.echo(f"  {c['column']:<{width}} {arrow} {c['field']:<{width + 3}}"
                       f"{c['type']:<8} e.g. {c['sample']}")
        if plan["generated_id"]:
            click.echo("  (no id column — ids will be generated)")
        first = next(iter(read_documents(Path(path))), None)
        if first:
            click.echo(f"\nfirst document:\n  {json.dumps(first, default=str)[:400]}")
        return

    spec = cl.load_spec()
    stats = asyncio.run(
        indexer.index_file(spec.base_url(), collection, path, threads, batch, commit_within,
                           engine=spec.engine)
    )
    click.echo(stats.summary())


# ------------------------------------------------------------------- load ---

@main.command()
@click.option("--collection", required=True)
@click.option("--rps", required=True, type=float, help="Target queries per second.")
@click.option("--duration", default="60", show_default=True, help="Test duration (90, 90s, 2m, 1h30m).")
@click.option("--ramp", default="0", show_default=True, help="Ramp linearly to target RPS over this long.")
@click.option("--queries", "queries_path", default=None, type=click.Path(exists=True), help="YAML query templates (default: q=*:*).")
@click.option("--index-rps", default=0.0, show_default=True, help="Concurrent single-doc index load (docs/s).")
@click.option("--index-profile", default=None, type=click.Path(exists=True), help="Data profile for concurrent index load.")
@click.option("--seed", default=None, type=int)
@click.option("--report", default=None, help="Write JSON report (with timeline) to this path.")
@click.option("--html", "html_out", default=None, help="Also write a self-contained HTML report.")
@click.option("--assert", "assertions", multiple=True, metavar="EXPR",
              help='Regression gate, repeatable: --assert "p99_ms<50" --assert "errors=0". '
                   'Exits 1 if any fail.')
def load(collection, rps, duration, ramp, queries_path, index_rps, index_profile, seed, report, html_out, assertions):
    """Run an open-loop load test against a collection."""
    for a in assertions:
        gates.parse_assertion(a)  # fail fast on typos, before the run
    duration = gates.parse_duration(duration)
    ramp = gates.parse_duration(ramp)
    spec = cl.load_spec()
    result = asyncio.run(
        loadtest.run_load(
            spec.base_url(), collection, rps, duration, ramp,
            queries_path, index_rps, index_profile, seed=seed,
            live_file=cl.WORKDIR / "live-load.json", engine=spec.engine,
        )
    )
    click.echo(result.summary())
    if result.records and all(not r.ok for r in result.records):
        click.echo("\nhint: every request failed — is the cluster healthy and the "
                   "collection name right? try `searchlab status` / `searchlab doctor`")
    if result.dropped:
        click.echo(
            "\nnote: dropped > 0 means the client hit its in-flight cap — "
            "the server (or client box) couldn't keep up with the schedule."
        )
    if html_out and not report:
        report = html_out.rsplit(".", 1)[0] + ".json"
    if report:
        loadtest.save_report(result, report)
        click.echo(f"report written to {report}")
    if html_out:
        rp.html_report(report, html_out)
        click.echo(f"HTML report written to {html_out}")
    _check_gates(result, assertions)


def _check_gates(result, assertions):
    if not assertions:
        return
    failures = gates.check_assertions(gates.result_metrics(result), list(assertions))
    if failures:
        click.echo("\nassertion failures:")
        for f in failures:
            click.echo(f"  FAIL {f}")
        raise SystemExit(1)
    click.echo(f"\nall {len(assertions)} assertion(s) passed")


# ------------------------------------------------------------------ chaos ---

@main.group()
def chaos():
    """Fault injection: crash, freeze, or restart nodes mid-test."""


@chaos.command("kill")
@click.argument("node")
def chaos_kill(node):
    """SIGKILL a node (hard crash, no graceful shutdown). NODE is e.g. solr2 or 2."""
    c = ch.kill(cl.load_spec(), node)
    click.echo(f"killed {c} — watch leader election / recovery with `searchlab status`")


@chaos.command("pause")
@click.argument("node")
def chaos_pause(node):
    """SIGSTOP a node: frozen JVM, mimics a huge GC pause or hung process."""
    c = ch.pause(cl.load_spec(), node)
    click.echo(f"paused {c} — unpause with `searchlab chaos unpause {node}`")


@chaos.command("unpause")
@click.argument("node")
def chaos_unpause(node):
    """Resume a paused node."""
    click.echo(f"unpaused {ch.unpause(cl.load_spec(), node)}")


@chaos.command("start")
@click.argument("node")
def chaos_start(node):
    """Start a killed node; watch it rejoin and recover replicas."""
    click.echo(f"started {ch.start(cl.load_spec(), node)}")


@chaos.command("restart")
@click.argument("node")
def chaos_restart(node):
    """Graceful restart (orderly shutdown, unlike kill)."""
    click.echo(f"restarted {ch.restart(cl.load_spec(), node)}")


@chaos.command("run")
@click.argument("scenario", type=click.Path(exists=True))
def chaos_run(scenario):
    """Execute a timed scenario YAML (steps with at/action/node) — a
    reproducible failure drill to run alongside a load test."""
    ch.run_scenario(cl.load_spec(), scenario, log=click.echo)


@main.command("drill")
@click.argument("drill_yaml", type=click.Path(exists=True))
@click.option("--out", default=None, help="Report basename (default: from the YAML's `report`, else 'drill').")
@click.option("--assert", "assertions", multiple=True, metavar="EXPR",
              help="Regression gate, repeatable; combined with the YAML's `assert:` list.")
def drill_cmd(drill_yaml, out, assertions):
    """Run an orchestrated failure drill: metrics snapshot, load with timed
    chaos injected mid-flight, snapshot again — one annotated HTML report."""
    spec = cl.load_spec()
    cfg = dr.load_drill(drill_yaml)
    outcome = asyncio.run(dr.run_drill(spec, cfg, log=click.echo))
    click.echo("\n" + outcome["result"].summary())
    base = out or cfg.get("report", "drill")
    json_path, html_path = dr.save_drill(outcome, base,
                                         title=f"searchlab drill · {Path(drill_yaml).stem}")
    click.echo(f"\nreport written to {json_path} and {html_path}")
    _check_gates(outcome["result"], list(assertions) + list(cfg.get("assert", [])))


# ---------------------------------------------------------------- metrics ---

@main.command("metrics")
@click.option("--out", default=None, help="Write full JSON snapshot to this path.")
@click.option("--watch", default=0, type=int, help="Refresh every N seconds (Ctrl-C to stop).")
def metrics_cmd(out, watch):
    """Snapshot heap, GC, cache hit ratios, and update/merge stats per node."""
    spec = cl.load_spec()
    while True:
        snap = metrics.snapshot_cluster(spec)
        click.echo(metrics.format_snapshot(snap))
        if out:
            metrics.save_snapshot(snap, out)
            click.echo(f"\nsnapshot written to {out}")
        if not watch:
            break
        click.echo("-" * 60)
        time.sleep(watch)


@main.command("metrics-diff")
@click.argument("before", type=click.Path(exists=True))
@click.argument("after", type=click.Path(exists=True))
def metrics_diff(before, after):
    """Diff two metrics snapshots: GC time burned, cache movement, merges."""
    a = json.loads(Path(before).read_text())
    b = json.loads(Path(after).read_text())
    click.echo(metrics.diff_snapshots(a, b))


@main.command()
@click.option("--port", default=8990, show_default=True)
@click.option("--demo", is_flag=True, help="Synthesized signals — preview the UI without a cluster.")
def dashboard(port, demo):
    """Live strip-chart dashboard: p99, heap, rate, caches, merges."""
    spec = cl.ClusterSpec() if demo else cl.load_spec()
    db.serve(spec, port=port, demo=demo)


# ----------------------------------------------------------------- schema ---

@main.command("schema")
@click.option("--collection", required=True)
@click.option("--profile", "profile_path", required=True, type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Print the field definitions without applying.")
@click.option("--engine", "engine_override", default=None,
              type=click.Choice(["solr", "elasticsearch", "es", "opensearch", "os"]),
              help="Only needed with --dry-run and no cluster (defaults to the cluster's engine).")
def schema_cmd(collection, profile_path, dry_run, engine_override):
    """Create explicit schema fields (Solr) or index mappings (ES/OS) from a
    data profile. Per-field overrides via `solr:` / `es:` blocks."""
    from .engines import get_engine
    profile = datagen.load_profile(profile_path)
    if dry_run and engine_override:
        spec, engine = None, get_engine(engine_override).name
    else:
        spec = cl.load_spec()
        engine = spec.engine
    if engine == "solr":
        click.echo(sc.apply_schema(spec, collection, profile, dry_run=dry_run))
    else:
        click.echo(sc.apply_mappings(spec, collection, profile, dry_run=dry_run,
                                     engine=engine))


@main.command()
@click.option("--engine", default="solr", show_default=True,
              type=click.Choice(["solr", "elasticsearch", "es", "opensearch", "os"]))
@click.option("--solr-version", "--version", "solr_version", default=None)
@click.option("--nodes", default=2, show_default=True)
@click.option("--collection", default="products", show_default=True)
@click.option("--count", default=10_000, show_default=True, help="Docs to generate and index.")
@click.option("--rps", default=25.0, show_default=True)
@click.option("--duration", default=30.0, show_default=True)
def quickstart(engine, solr_version, nodes, collection, count, rps, duration):
    """Zero-to-load-test in one command: up, collection, gen, index, load."""
    from .engines import get_engine

    eng = get_engine(engine)
    defaults = {"solr": "9.6", "elasticsearch": "8.14.3", "opensearch": "2.15.0"}
    version = solr_version or defaults[eng.name]
    spec = cl.ClusterSpec(engine=eng.name, solr_version=version, solr_nodes=nodes,
                          base_port=8983 if eng.name == "solr" else eng.default_port)
    click.echo(f"[1/5] starting {nodes}-node {eng.name} {version}...")
    cl.up(spec)
    click.echo(f"[2/5] creating collection '{collection}' ({nodes} shard(s))...")
    cl.create_collection(spec, collection, shards=nodes, replicas=1)

    profile = Path("profiles/default.yaml")
    if not profile.exists():  # installed without the repo checkout
        cl.WORKDIR.mkdir(exist_ok=True)
        profile = cl.WORKDIR / "default-profile.yaml"
        profile.write_text(
            "fields:\n  id: {type: id}\n"
            "  title_t: {type: text, min_words: 3, max_words: 10}\n"
            "  body_t: {type: text, min_words: 50, max_words: 200}\n"
            "  category_s: {type: categorical, cardinality: 50, zipf: 1.1}\n"
            "  price_f: {type: float, min: 1, max: 5000}\n"
        )
    data = cl.WORKDIR / "quickstart-data.jsonl"
    click.echo(f"[3/5] generating {count} docs from {profile}...")
    datagen.generate_to_file(profile, count, data, seed=42)
    click.echo("[4/5] indexing...")
    stats = asyncio.run(indexer.index_file(spec.base_url(), collection, data, threads=4,
                                           engine=spec.engine))
    click.echo(f"      {stats.summary()}")
    click.echo(f"[5/5] load test: {rps} rps for {duration}s...")
    result = asyncio.run(loadtest.run_load(
        spec.base_url(), collection, rps, duration, ramp=min(10, duration / 3),
        live_file=cl.WORKDIR / "live-load.json", engine=spec.engine,
    ))
    click.echo(result.summary())
    click.echo(f"\ncluster is still up — try:\n"
               f"  searchlab dashboard          (live recorder UI)\n"
               f"  searchlab load --collection {collection} --rps {rps * 4} --duration 120\n"
               f"  searchlab chaos pause solr2  (then watch the dashboard)\n"
               f"  searchlab down               (when finished)")


# ----------------------------------------------------------------- replay ---

@main.command()
@click.option("--collection", required=True)
@click.option("--file", "log_path", required=True, type=click.Path(exists=True),
              help="Solr request log, or plain file with one query string per line.")
@click.option("--speed", default=1.0, show_default=True,
              help="Time-scale the original pacing (2.0 = replay twice as fast).")
@click.option("--rps", default=None, type=float,
              help="Ignore log timing; replay uniformly at this rate.")
@click.option("--loop", "loop_count", default=1, show_default=True,
              help="Repeat the log N times.")
@click.option("--path-filter", default="/select", show_default=True,
              help="Only replay requests to this handler.")
@click.option("--report", default=None, help="Write JSON report to this path.")
@click.option("--html", "html_out", default=None, help="Also write an HTML report.")
@click.option("--assert", "assertions", multiple=True, metavar="EXPR",
              help='Regression gate, repeatable (see `load --help`).')
def replay(collection, log_path, speed, rps, loop_count, path_filter, report, html_out, assertions):
    """Replay real query traffic — Solr request logs, plain query files, or
    ES/OS search slow logs (classic or JSON-lines) — at original, scaled, or
    fixed pacing. Format is chosen by the running cluster's engine."""
    spec = cl.load_spec()
    entries = rpl.parse_log(log_path, path_filter=path_filter, engine=spec.engine)
    span = entries[-1]["offset_s"]
    pacing = f"{rps} rps uniform" if rps else f"original pacing / {speed}x"
    click.echo(f"replaying {len(entries)} queries (log spans {span:.0f}s) — {pacing}, "
               f"{loop_count} loop(s)")
    result = asyncio.run(rpl.replay(
        spec.base_url(), collection, entries, speed=speed, rps=rps, loop_count=loop_count))
    click.echo(result.summary())
    if html_out and not report:
        report = html_out.rsplit(".", 1)[0] + ".json"
    if report:
        loadtest.save_report(result, report)
        click.echo(f"report written to {report}")
    if html_out:
        rp.html_report(report, html_out, title="searchlab replay report")
        click.echo(f"HTML report written to {html_out}")
    _check_gates(result, assertions)


# ------------------------------------------------------------------ gclog ---

@main.command()
@click.argument("target", default="")
@click.option("--html", "html_out", default=None,
              help="Write an HTML report with per-node pause timelines.")
def gclog(target, html_out):
    """Analyze JVM GC logs: pause tail, throughput lost, Full GC detection.

    TARGET is a node name (solr2), a log file path, or empty for all nodes
    under ./.searchlab/gc-logs (requires `searchlab up --gc-logs`)."""
    if target and Path(target).is_file():
        by_node = {Path(target).name: gcl.parse_gclog(target)}
    else:
        logs = gcl.find_gclogs(cl.WORKDIR / "gc-logs")
        if target:
            logs = {k: v for k, v in logs.items() if k == target}
            if not logs:
                raise SystemExit(f"searchlab: no GC logs for node '{target}'")
        by_node = {}
        for node, files in logs.items():
            pauses = []
            for f in files:
                pauses.extend(gcl.parse_gclog(f))
            pauses.sort(key=lambda p: p.uptime_s)
            by_node[node] = pauses
    for node, pauses in by_node.items():
        click.echo(gcl.summarize(pauses, label=node))
        click.echo()
    if html_out:
        rp.html_gc(by_node, html_out)
        click.echo(f"HTML report written to {html_out}")


# ----------------------------------------------------------------- doctor ---

@main.command()
def doctor():
    """Preflight checks: docker, compose, ports, disk, leftover state."""
    import shutil as sh
    import socket
    import subprocess

    failures = 0

    def check(name: str, ok: bool, hint: str = "") -> None:
        nonlocal failures
        mark = "ok " if ok else "FAIL"
        click.echo(f"[{mark}] {name}" + (f" — {hint}" if not ok and hint else ""))
        failures += 0 if ok else 1

    docker = sh.which("docker") is not None
    check("docker on PATH", docker, "install Docker: https://docs.docker.com/get-docker/")
    if docker:
        r = subprocess.run(["docker", "info"], capture_output=True)
        check("docker daemon reachable", r.returncode == 0, "is Docker running?")
        r = subprocess.run(["docker", "compose", "version"], capture_output=True)
        check("compose plugin", r.returncode == 0 or sh.which("docker-compose") is not None,
              "install the docker compose plugin")
    for port, what in [(8983, "solr1"), (8984, "solr2"), (8990, "dashboard")]:
        with socket.socket() as s:
            free = s.connect_ex(("127.0.0.1", port)) != 0
        check(f"port {port} free ({what})", free,
              "something is already listening — a leftover cluster? try `searchlab down`")
    du = sh.disk_usage("/")
    check("disk space > 5 GB", du.free > 5 * 2**30, f"only {du.free / 2**30:.1f} GB free")
    spec_file = cl.WORKDIR / "spec.json"
    if spec_file.exists():
        click.echo(f"[note] existing cluster state in {cl.WORKDIR} — `searchlab status` to inspect")
    click.echo("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
    if failures:
        raise SystemExit(1)


@main.command("k8s")
@click.option("--engine", default=None,
              type=click.Choice(["solr", "elasticsearch", "es", "opensearch", "os"]),
              help="Defaults to the running cluster's engine, else solr.")
@click.option("--version", "version", default=None, help="Image/stack version.")
@click.option("--nodes", default=None, type=int)
@click.option("--zk-nodes", default=None, type=int, help="Solr only.")
@click.option("--heap", default=None)
@click.option("--gc-tune", default=None)
@click.option("--out", default=None, help="Write to a file instead of stdout.")
def k8s_cmd(engine, version, nodes, zk_nodes, heap, gc_tune, out):
    """Export a Kubernetes operator manifest (SolrCloud / ECK / OpenSearch CR)
    mirroring the lab spec. Defaults come from the running cluster if any."""
    from .engines import get_engine
    try:
        spec = cl.load_spec()
    except SystemExit:
        spec = cl.ClusterSpec()
    if engine:
        spec.engine = get_engine(engine).name
        defaults = {"solr": "9.6", "elasticsearch": "8.14.3", "opensearch": "2.15.0"}
        spec.solr_version = defaults[spec.engine]
    for attr, val in (("solr_version", version), ("solr_nodes", nodes),
                      ("zk_nodes", zk_nodes), ("heap", heap), ("gc_tune", gc_tune)):
        if val is not None:
            setattr(spec, attr, val)
    manifest = k8.render_k8s(spec)
    if out:
        Path(out).write_text(manifest)
        click.echo(f"manifest written to {out}")
    else:
        click.echo(manifest)


@main.command("sweep")
@click.argument("sweep_yaml", type=click.Path(exists=True))
@click.option("--out", default=None, help="Report basename (default: YAML's `report`, else 'sweep').")
def sweep_cmd(sweep_yaml, out):
    """Run one workload across a matrix of cluster configs (fresh cluster per
    cell) and produce a comparison matrix. Expect minutes per cell."""
    cfg = sw.load_sweep(sweep_yaml)
    n = len(sw.cells(cfg))
    click.confirm(f"{n} cell(s) = {n} full cluster up/down cycles. Continue?",
                  abort=True, default=True)
    results = sw.run_sweep(cfg, log=click.echo)
    json_path, html_path = sw.save_sweep(results, cfg, out or cfg.get("report", "sweep"))
    click.echo(f"\nmatrix written to {json_path} and {html_path}")


@main.command("recall")
@click.option("--collection", required=True)
@click.option("--profile", "profile_path", required=True, type=click.Path(exists=True),
              help="The profile the data was generated from (locates the vector field).")
@click.option("--data", "data_path", required=True, type=click.Path(exists=True),
              help="The generated JSONL you indexed (ground truth is computed from it).")
@click.option("--queries", "n_queries", default=100, show_default=True)
@click.option("--k", default=10, show_default=True)
@click.option("--candidates", default=None,
              help="ES num_candidates values to sweep, comma-separated (e.g. 20,50,100,500).")
@click.option("--seed", default=1337, show_default=True,
              help="Query-vector seed (keep it different from the data seed).")
@click.option("--report", default=None, help="Write results JSON to this path.")
def recall_cmd(collection, profile_path, data_path, n_queries, k, candidates, seed, report):
    """Measure ANN recall@k against locally computed exact ground truth,
    with latency — across a num_candidates curve where the engine has one."""
    spec = cl.load_spec()
    profile = datagen.load_profile(profile_path)
    field, field_cfg = rc.find_vector_field(profile)
    ids, data = rc.load_vectors(data_path, field)
    click.echo(f"{len(ids)} dataset vectors, {n_queries} queries, k={k} — "
               f"computing exact ground truth...")
    qs = rc.gen_query_vectors(field_cfg, n_queries, seed)
    truth = rc.ground_truth(data, qs, k)

    cand_list = [None]
    if candidates:
        if spec.engine != "elasticsearch":
            click.echo("note: --candidates sweeps ES num_candidates; "
                       f"{spec.engine} uses its engine default per query")
        else:
            cand_list = [int(c) for c in str(candidates).split(",")]
    results = []
    for cand in cand_list:
        results.append(asyncio.run(rc.run_recall(
            spec.engine, spec.base_url(), collection, field,
            ids, qs, truth, k, candidates=cand)))
    click.echo("\n" + rc.format_results(results, k))
    if any(r["recall_mean"] is not None and r["recall_mean"] < 0.9 for r in results):
        click.echo("\nnote: recall < 0.9 — raise num_candidates/ef_search or the "
                   "HNSW build knobs (hnswBeamWidth / ef_construction) and re-test")
    if report:
        Path(report).write_text(json.dumps({"k": k, "results": results}, indent=2))
        click.echo(f"results written to {report}")


@main.command("learn")
@click.argument("lesson", default="")
def learn_cmd(lesson):
    """Interactive lessons against your LIVE cluster — kill nodes, index
    docs, answer questions; the lesson watches real state change."""
    lessons = ln.builtin_lessons()
    if not lesson:
        click.echo("available lessons:\n")
        for name, les in sorted(lessons.items()):
            click.echo(f"  {name:<26} {les['title']}")
            if les.get("requires"):
                click.echo(f"  {'':<26} requires: {les['requires']}")
        click.echo("\nrun one: searchlab learn <name>")
        return
    if lesson not in lessons:
        raise SystemExit(f"searchlab: no lesson '{lesson}' — run `searchlab learn` to list")
    spec = cl.load_spec()
    ln.run_lesson(ln.load_lesson(lessons[lesson]), spec.base_url())


@main.command("explain")
@click.argument("query_string")
@click.option("--collection", required=True)
def explain_cmd(query_string, collection):
    """Run a query with debug=true and translate the output: what the query
    became, where the time went, why the top doc scored. QUERY_STRING is
    e.g. "q=title_t:merge&fq=category_s:x". Solr only."""
    spec = cl.load_spec()
    if spec.engine != "solr":
        raise SystemExit("searchlab: explain reads Solr's debug component; "
                         f"the running cluster is {spec.engine}")
    body = ex.fetch_debug(spec.base_url(), collection, query_string)
    click.echo(ex.explain_report(body))


# ---------------------------------------------------------------- reports ---

@main.command()
@click.argument("report_a", type=click.Path(exists=True))
@click.argument("report_b", type=click.Path(exists=True))
@click.option("--html", "html_out", default=None, help="Write an HTML comparison to this path.")
def compare(report_a, report_b, html_out):
    """Diff two load-test JSON reports (A/B, e.g. across Solr versions)."""
    click.echo(rp.compare_text(report_a, report_b))
    if html_out:
        rp.html_compare(report_a, report_b, html_out)
        click.echo(f"\nHTML comparison written to {html_out}")


@main.command("report-html")
@click.argument("report_json", type=click.Path(exists=True))
@click.option("--out", default=None, help="Output path (default: <report>.html).")
def report_html(report_json, out):
    """Render a JSON load report as a self-contained HTML page with charts."""
    out = out or report_json.rsplit(".", 1)[0] + ".html"
    rp.html_report(report_json, out)
    click.echo(f"HTML report written to {out}")


# --------------------------------------------------------------- scenario ---

def _scenario_spec():
    """The running cluster, or Solr defaults so `list`/`show` work offline."""
    if (cl.WORKDIR / "spec.json").exists():
        return cl.load_spec(), True
    return cl.ClusterSpec(), False


def _echo_watch(watch):
    if not watch:
        return
    click.echo("\nwhat to watch:")
    for w in watch:
        click.echo(f"  - {w}")


@main.group("scenario")
def scenario_grp():
    """Named reproductions: a data shape, a query mix, faults, and what to watch.

    A scenario is a repro recipe as an object — the recipes the README used to
    describe in prose, runnable in one command.
    """


@scenario_grp.command("list")
def scenario_list():
    """List the available scenarios."""
    items = scn.catalog()
    if not items:
        click.echo("searchlab: no scenarios found — expected a ./scenarios directory")
        return
    width = max(len(s["name"]) for s in items)
    for s in items:
        if s["error"]:
            click.echo(f"  {s['name']:<{width}}  (unreadable: {s['error']})")
        else:
            click.echo(f"  {s['name']:<{width}}  {s['title']}")
    click.echo(f"\n{len(items)} scenario(s) — searchlab scenario show <name> for detail")


@scenario_grp.command("show")
@click.argument("name")
def scenario_show(name):
    """Explain a scenario: what it does, what it wants, and what to watch."""
    spec, live = _scenario_spec()
    cfg = scn.load(name)
    p = scn.plan(cfg, spec)

    click.echo(f"{p['name']} — {p['title']}\n")
    if cfg.get("about"):
        click.echo(cfg["about"].rstrip())
    click.echo(f"\nengine:     {p['engine']}" + ("" if live else "  (no cluster up — showing defaults)"))
    click.echo(f"collection: {p['collection']}")
    click.echo(f"data:       {p['count']:,} docs from {p['profile']}"
               + (f", seed {p['seed']}" if p["seed"] is not None else ""))
    click.echo(f"queries:    {p['queries']}")
    click.echo(f"load:       {p['rps']} rps for {p['duration']}s"
               + (f", plus {p['index_rps']} doc/s indexing" if p["index_rps"] else ""))
    if p["chaos"]:
        click.echo("chaos:")
        for step in p["chaos"]:
            click.echo(f"  t={step['at']:>4}s  {step['action']} {step['node']}")
    if live and p["warnings"]:
        click.echo("\ncluster mismatch:")
        for w in p["warnings"]:
            click.echo(f"  ! {w}")
    if p["missing"]:
        click.echo("\nmissing files (scenarios reference the checkout's "
                   "profiles/ and queries/):")
        for f in p["missing"]:
            click.echo(f"  ? {f}")
    _echo_watch(p["watch"])


@scenario_grp.command("run")
@click.argument("name")
@click.option("--collection", default=None, help="Override the scenario's collection name.")
@click.option("--count", default=None, type=int, help="Override the document count.")
@click.option("--skip-setup", is_flag=True, help="Reuse what is already indexed; go straight to load.")
@click.option("--dry-run", is_flag=True, help="Print the plan and touch nothing.")
@click.option("--out", default=None, help="Report basename (default: the scenario's `report`).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--assert", "assertions", multiple=True, metavar="EXPR",
              help="Extra regression gate; combined with the scenario's `assert:` list.")
def scenario_run(name, collection, count, skip_setup, dry_run, out, yes, assertions):
    """Run a named scenario end to end and write an annotated report."""
    cfg = scn.load(name)
    if collection:
        cfg["data"]["collection"] = collection
    if count is not None:
        cfg["data"]["count"] = count

    if not (cl.WORKDIR / "spec.json").exists():
        if not dry_run:
            raise SystemExit("searchlab: no cluster — run `searchlab up` first "
                             "(or `searchlab scenario run --dry-run` to see the plan)")
        spec = cl.ClusterSpec()
    else:
        spec = cl.load_spec()
    p = scn.plan(cfg, spec)

    click.echo(f"{p['name']} — {p['title']}\n")
    if cfg.get("about"):
        click.echo(cfg["about"].rstrip() + "\n")
    for w in p["warnings"]:
        click.echo(f"  ! {w}")

    blocking = [f for f in p["missing"]
                if not (skip_setup and f == p["profile"])]
    if blocking:
        raise SystemExit(
            "searchlab: scenario references files that are not here: "
            + ", ".join(blocking)
            + "\n  scenarios point at profiles/ and queries/ relative to the "
              "repo checkout — run from there, or set $SEARCHLAB_SCENARIOS to "
              "your own scenario directory")

    setup_line = ("reusing what is already in the collection"
                  if skip_setup else
                  f"index {p['count']:,} docs from {p['profile']} into '{p['collection']}'")
    click.echo(f"\nplan: {setup_line}, then {p['rps']} rps for {p['duration']}s"
               + (f" with {len(p['chaos'])} chaos step(s)" if p["chaos"] else "")
               + f" against {p['engine']}")

    if dry_run:
        _echo_watch(p["watch"])
        click.echo(f"\ndry run — nothing was touched. Report would be written to {p['report']}.json/.html")
        return

    if not yes:
        click.confirm("continue?", abort=True, default=True)

    if not skip_setup:
        click.echo(f"\n[1/3] collection '{p['collection']}'...")
        try:
            cl.create_collection(spec, p["collection"], shards=spec.solr_nodes, replicas=1)
        except Exception as e:  # already there is the common, harmless case
            click.echo(f"      (using the existing collection: {e})")
        data_path = cl.WORKDIR / f"scenario-{p['name']}.jsonl"
        click.echo(f"[2/3] generating {p['count']:,} docs -> {data_path}...")
        datagen.generate_to_file(p["profile"], p["count"], data_path, seed=p["seed"])
        stats = asyncio.run(indexer.index_file(spec.base_url(), p["collection"], data_path,
                                               threads=4, engine=spec.engine))
        click.echo(f"      {stats.summary()}")
    click.echo("[3/3] load" + (" with chaos" if p["chaos"] else "") + "...")

    _echo_watch(p["watch"])
    click.echo("")

    outcome = asyncio.run(dr.run_drill(spec, scn.to_drill_cfg(cfg, spec), log=click.echo))
    click.echo("\n" + outcome["result"].summary())

    base = out or p["report"]
    json_path, html_path = dr.save_drill(outcome, base,
                                         title=f"searchlab scenario · {p['name']}")
    click.echo(f"\nreport written to {json_path} and {html_path}")
    _echo_watch(p["watch"])
    _check_gates(outcome["result"], list(assertions) + list(cfg.get("assert", [])))


if __name__ == "__main__":
    main()
