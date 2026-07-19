# searchlab

Disposable search clusters — **SolrCloud, Elasticsearch, or OpenSearch** — with synthetic data of controllable shape and open-loop load tests. Built for two jobs: **learning search-engine internals** and **reproducing production performance issues** on demand. (Yes, it outgrew its name. Names are forever; scope isn't.)

```
pip install -e .
searchlab doctor            # preflight: docker, ports, disk
searchlab quickstart        # or: quickstart --engine opensearch | elasticsearch
                          # or step by step:
searchlab up --solr-version 9.6 --nodes 2 --heap 1g
searchlab create-collection products --shards 2 --replicas 1
searchlab gen --profile profiles/default.yaml --count 10000 --out data.jsonl
searchlab index --collection products --file data.jsonl --threads 4
searchlab load --collection products --rps 50 --duration 60 --ramp 15 --queries queries/default.yaml
searchlab down
```

Requires Docker (with the compose plugin) and Python 3.10+.

## Engines

```
searchlab up --engine solr --solr-version 9.6 --nodes 3        # the default
searchlab up --engine elasticsearch --version 8.14.3 --nodes 2
searchlab up --engine opensearch --version 2.15.0 --nodes 2
```

One CLI, one dashboard, one report format. Everything downstream — `gen`, `index`, `load` (with `queries/es-default.yaml` DSL templates and `{RAND_WORD}` substitution anywhere in the body), `chaos`, `metrics`, `metrics-diff`, `compare`, `dashboard`, `quickstart` — works across all three engines; the cluster remembers its engine in `.searchlab/spec.json`. Solr-only for now: `--monitoring` (Prometheus stack), `schema`, `replay`, `gclog`, and server-side p99/rate on the dashboard (ES/OS stats expose totals, not percentiles — heap, GC, caches, docs, and merges all stream normally).

`schema` and `replay` are engine-aware too: on ES/OS clusters, `schema` applies index mappings derived from the same profile (per-field `es:` overrides, e.g. `doc_values: false` for fielddata-pressure repros), and `replay` parses search slow logs — both the classic bracketed format and JSON-lines — and replays the recorded query bodies.

Same seed + same profile + same load against Solr and OpenSearch, then `compare` the reports: cross-engine benchmarking in four commands.

## Why another load tool

Most load tools are **closed-loop**: they wait for a response before sending the next request. When the server slows down, the client slows down with it, and the worst latencies — the ones that caused the incident you're reproducing — never get measured. This is coordinated omission.

searchlab is **open-loop**: requests fire on a fixed wall-clock schedule derived from the target RPS. If the server can't keep up, latency climbs and you see it. If the client saturates its in-flight cap, requests are counted as `dropped` instead of silently queued.

## Commands

| Command | What it does |
|---|---|
| `up` | Render docker-compose and start SolrCloud (+ ZK, optionally Prometheus/Grafana) |
| `down` | Tear down (`--volumes` to wipe data) |
| `status` | Live nodes and collection health |
| `create-collection` / `delete-collection` | Collection management |
| `gen` | Generate JSONL docs from a YAML profile |
| `index` | Async bulk indexing with `--threads` and `--batch` |
| `load` | Open-loop load test with ramping, mixed workloads, `--report` / `--html` |
| `chaos kill/pause/unpause/start/restart` | Fault injection on individual nodes |
| `chaos run scenario.yaml` | Timed fault steps only (see `examples/chaos-node-loss.yaml`) |
| `drill drill.yaml` | Full orchestrated drill: load + chaos + metrics, one annotated report |
| `sweep sweep.yaml` | One workload x a config matrix, fresh cluster per cell, comparison table |
| `dashboard` | Live strip-chart UI: load test, p99, heap sawtooth, rate, caches, merges (`--demo` to preview) |
| `schema` | Explicit schema fields derived from a data profile (`--dry-run` to inspect) |
| `quickstart` | Zero-to-load-test in one command: up + collection + gen + index + load |
| `replay` | Replay real Solr request logs at original, scaled, or fixed pacing |
| `gclog` | GC pause analysis: tail percentiles, throughput lost, Full GC detection |
| `doctor` | Preflight checks: docker, compose plugin, ports, disk, leftover state |
| `k8s` | Export Kubernetes operator manifests (SolrCloud / ECK / OpenSearch CRs) |
| `metrics` | Heap, GC, cache hit ratios, merge/commit stats per node (`--watch N` to poll) |
| `metrics-diff` | Before/after story of a run: GC time burned, cache movement, merges |
| `compare` | Diff two JSON reports (A/B across versions or configs), `--html` for charts |
| `report-html` | Render a JSON report as a dependency-free HTML page with SVG charts |

State lives in `./.searchlab/` (generated compose file + cluster spec), so every command after `up` knows where the cluster is.

## Chaos: learning SolrCloud failure modes

`kill` is SIGKILL — a hard crash with no goodbye to ZooKeeper. `pause` is nastier and more realistic: SIGSTOP freezes the JVM in place, which is exactly what a monster GC pause or a hung process looks like to the rest of the cluster. Run a load test in one terminal and inject faults in another:

```
searchlab load --collection products --rps 50 --duration 300 --report chaos-run.json &
searchlab chaos pause solr2       # watch p99 and errors in the timeline
sleep 45
searchlab chaos unpause solr2
searchlab status                  # replica states, recovery
```

## Dashboard

```
searchlab dashboard            # http://localhost:8990, polls the live cluster
searchlab dashboard --demo     # synthesized signals, preview without a cluster
```

A single self-contained page (no CDN, no build step) styled as a strip-chart recorder on millimeter graph paper: pen traces for p99 latency (red), per-node heap sawtooth (blue), and query rate (green) over a 5-minute window, plus node nameplates with heap gauges and GC counters, cache hit-ratio meters, and update/merge counters.

**Live load-test streaming:** while `searchlab load` runs, it writes rolling stats to `.searchlab/live-load.json` and the dashboard's top panel comes alive automatically — client-observed p50/p99 traces, target vs achieved RPS, progress, errors, dropped. The client-side p99 next to the server-side `/select` p99 is the whole story of a saturation event on one screen: queueing shows up in the client trace before the server metric moves.

## Metrics before/after

```
searchlab metrics --out before.json
searchlab load --collection products --rps 100 --duration 120
searchlab metrics --out after.json
searchlab metrics-diff before.json after.json   # GC ms burned, hitratio drift, merges, adds
searchlab metrics --watch 5                     # or poll live during a run
```

## Failure drills

The one-command version — everything orchestrated from a single YAML:

```
searchlab drill examples/drill-node-loss.yaml
```

```yaml
collection: products
seed: 42
load:  { rps: 50, duration: 240, ramp: 15, index_rps: 10, queries: queries/default.yaml }
chaos:
  - { at: 60,  action: pause,   node: solr2 }   # frozen JVM: the realistic failure
  - { at: 120, action: unpause, node: solr2 }
  - { at: 150, action: kill,    node: solr2 }
  - { at: 200, action: start,   node: solr2 }
report: drill-node-loss
```

It snapshots metrics, runs the load with the faults injected mid-flight on schedule, snapshots again, and writes one self-contained HTML report: the latency timeline **with each fault drawn as an annotated marker**, the fault table, summary, latency histogram, and the before/after metrics diff (GC time burned, cache movement, merges). Deterministic load + deterministic faults + one seed = run the identical drill on two versions or two engines and diff the reports.

(For faults without the orchestration, `searchlab chaos run scenario.yaml` still runs timed steps standalone alongside anything.)

## Cluster knobs

```
searchlab up \
  --solr-version 8.11.2 \      # pin the exact version from the incident
  --nodes 3 --zk-nodes 3 \
  --heap 512m \                # small heap = fast GC repro
  --gc-tune "-XX:+UseG1GC -XX:MaxGCPauseMillis=50" \
  --solr-opts "-Dsolr.autoSoftCommit.maxTime=1000" \
  --gc-logs \                  # GC logs land in ./.searchlab/gc-logs/<node>
  --monitoring                 # solr-exporter + Prometheus :9090 + Grafana :3000
```

## Data profiles

Real Solr performance problems come from data *shape*, not doc count. Profiles are YAML; each field declares a type and its distribution knobs:

- `text` — `min_words` / `max_words` (doc size, analysis cost)
- `categorical` — `cardinality` + optional `zipf` skew (facet/filter behavior)
- `keyword` — random strings (`length`), effectively unique at scale
- `multivalued` — wraps any inner type, `min_values` / `max_values`
- `int`, `float`, `date`, `bool`, `id`

`profiles/default.yaml` is an e-commerce-ish baseline. `profiles/high-cardinality.yaml` is a repro profile for high-cardinality faceting pain. Use `--seed` for reproducible datasets.

## Query templates

`queries/default.yaml` defines a weighted mix. `{RAND_WORD}` and `{RAND_INT:lo:hi}` placeholders are substituted per request so `queryResultCache` doesn't fake your numbers. Add a template with `weight: 0` to keep it on file but disabled (see the deep-paging antipattern example).

## Mixed read/write load

Merge- and cache-related pathologies show up under *concurrent* query and index load, not either alone:

```
searchlab load --collection products --rps 100 --duration 300 \
  --index-rps 20 --index-profile profiles/default.yaml \
  --report run1.json
```

The JSON report includes a per-5s timeline (`rps`, `p50`, `p99`, `errors`) — useful for spotting the sawtooth of commit/merge cycles.

## Learning interactively

```
searchlab learn                     # list lessons
searchlab learn leader-election     # run one
```

Lessons run against your **live cluster**, not a slideshow. The engine's signature move is the `wait` step: the lesson tells you to go do something real — `searchlab chaos kill solr2` in another terminal — then polls actual cluster state until ZooKeeper notices, and continues the story from what just happened. Multiple-choice questions (scored, with explanations either way) check the mental model along the way. Built-ins: **cluster-anatomy** (nodes/shards/replicas against your real topology), **leader-election** (you cause one), and **commits-and-visibility** (reproduces the classic "I indexed it, where is it?" surprise, then resolves it). Lessons are plain YAML — writing your own for a team onboarding is a text file away.

```
searchlab explain --collection products "q=title_t:Merging&fq=category_s:x"
```

`explain` runs the query with `debug=true` and translates: what your query *became* after the analysis chain (the "you wrote / solr ran" diff makes stemming and synonyms visible), where the time went per search component with the dominant one flagged, and the top document's score tree pruned to the readable part.

## Vector search

The full loop works for dense vectors on all three engines:

```
searchlab up --heap 2g
searchlab create-collection vecs
searchlab schema --collection vecs --profile profiles/vectors.yaml   # DenseVectorField + HNSW knobs
searchlab gen --profile profiles/vectors.yaml --count 200k --out vecs.jsonl
searchlab index --collection vecs --file vecs.jsonl --threads 4      # watch HNSW build cost live
searchlab load --collection vecs --rps 30 --duration 2m --queries queries/vector-solr.yaml
```

Profiles declare `type: vector` with `dims`, `similarity`, and a clustered shape (`clusters`, `cluster_std`) — real embeddings cluster, and uniform random vectors are pathological for ANN, so the generator builds centroids and samples around them, seeded and deterministic. On Solr, `schema` auto-creates the `DenseVectorField` fieldType with HNSW knobs from the `solr:` block; on ES it's `dense_vector`, on OS `knn_vector` (and OS lab indexes are created with `index.knn: true`).

**Recall — the other half.** Latency without recall is meaningless for ANN; HNSW will happily answer fast and wrong. `recall` computes exact ground truth locally over the dataset you generated (brute force via numpy — `pip install 'searchlab[recall]'`), queries the engine's approximate kNN with in-distribution query vectors, and reports recall@k with latency:

```
searchlab recall --collection vecs --profile profiles/vectors.yaml --data vecs.jsonl \
  --k 10 --queries 200 --candidates 20,50,100,500
```

On ES the `--candidates` sweep produces the recall/latency tradeoff curve — the plot HNSW tuning actually happens on. Recall under 0.9 gets a hint pointing at `num_candidates` and the build-time knobs.

Query templates use `{RAND_VECTOR:dims}` — a fresh normalized vector per request (kNN caches can't fake your numbers either). It becomes a real JSON array in ES/OS bodies and bracketed text inside Solr's `{!knn f=vec topK=10}` syntax. The shipped templates include the two classic latency levers: deep `topK`/`num_candidates` and pre-filtered kNN. And since vectors are just profile fields, everything composes: `sweep` HNSW build knobs or heap sizes under vector load, `drill` a node loss mid-kNN, gate `p99_ms` in CI.

## Replaying production traffic

Synthetic templates answer "how does it behave under load"; a replay answers "what happened last Tuesday". Feed it a standard Solr request log (the `o.a.s.c.S.Request` lines — non-request lines are skipped automatically) or a plain file with one query string per line:

```
searchlab replay --collection products --file solr.log                 # original pacing
searchlab replay --collection products --file solr.log --speed 4       # 4x faster
searchlab replay --collection products --file solr.log --rps 80 --loop 5
searchlab replay --collection products --file solr.log --path-filter /update
```

URL-encoded params are decoded, repeated keys (multiple `fq`s) preserved, and the output is a normal load report — so `--report`, `--html`, and `compare` all work. Replay the same incident log against two Solr versions and diff.

## GC log analysis

```
searchlab up --heap 512m --gc-logs          # mounts .searchlab/gc-logs/<node>/
# ... run load ...
searchlab gclog                              # all nodes
searchlab gclog solr2                        # one node
searchlab gclog path/to/solr_gc.log          # any unified-logging GC log (JDK 9+)
```

Reports pause counts and tail percentiles per pause type, total throughput lost to GC, average reclaim per collection, the single worst pause — and flags any Full GC, because in a healthy cluster there shouldn't be one. `--html` adds a per-node pause timeline on a square-root scale (young pauses stay visible next to Full GCs, which get annotated markers). Works on ES/OS clusters too — the same `--gc-logs` flag mounts their `gc.log*` files.

## Kubernetes export

```
searchlab k8s > lab.yaml                         # mirrors the running cluster
searchlab k8s --engine os --nodes 3 --heap 2g    # or from flags, no cluster needed
```

Emits the idiomatic operator CR for the engine — SolrCloud (Apache Solr Operator), Elasticsearch (ECK, with TLS/auth disabled to mirror the lab compose setup), or OpenSearchCluster (OpenSearch Operator) — with the operator install commands in the header comment. Version, node count, heap, and GC flags translate to each operator's shape; `kubectl apply` and you're on k8s.

## Explicit schema from a profile

Dynamic fields (`*_t`, `*_s`, ...) cover quick labs; `schema` creates explicit fields when the repro depends on per-field settings:

```
searchlab schema --collection events --profile profiles/high-cardinality.yaml --dry-run
searchlab schema --collection events --profile profiles/high-cardinality.yaml
```

Text maps to `text_general`; string/numeric/date fields get `docValues: true` by default. Override per field with a `solr:` block in the profile — e.g. `solr: { docValues: false }` on a facet field reproduces fieldCache heap pressure on demand.

## Config sweeps

"How much heap does this workload need?" — the question a lab exists to answer:

```
searchlab sweep examples/heap-sweep.yaml
```

Declares a config `matrix` (any of `heap`, `gc_tune`, `solr_opts`, `solr_version`, `solr_nodes`, `engine`) and a seeded workload; every combination gets a **fresh cluster** (up, index, load, tear down — teardown guaranteed even on failure) so cells can't contaminate each other, while the generated dataset is built once and reused. Output is a JSON + HTML comparison matrix with the best cell per metric highlighted and indexing throughput per cell. Expect minutes per cell — that's the price of clean cells.

## CI regression gates

Any `load`, `replay`, or `drill` can gate a build:

```
searchlab load --collection products --rps 100 --duration 2m \
  --assert "p99_ms<80" --assert "errors=0" --assert "achieved_rps>95"
```

Assertions are validated before the run starts (typos fail fast, not after two minutes of load) and evaluate against the same metric names the JSON report uses: `requests`, `errors`, `dropped`, `achieved_rps`, `p50_ms`, `p90_ms`, `p99_ms`, `p999_ms`. Exit code 1 on any failure. Drill YAMLs can carry their own `assert:` list, so a versioned drill file *is* the regression contract. Durations accept `90s` / `2m` / `1h30m`; counts accept `10k` / `1.5m`.

## Repro recipes

**GC pressure from faceting:**
```
searchlab up --heap 512m --gc-logs
searchlab create-collection events
searchlab gen --profile profiles/high-cardinality.yaml --count 500000 --out events.jsonl
searchlab index --collection events --file events.jsonl --threads 8
# facet on user_id_s at load, watch GC logs / Grafana
```

**Commit storm:** set `--solr-opts "-Dsolr.autoSoftCommit.maxTime=500"` and run `load` with `--index-rps 50`.

**Version regression:** run the identical `gen`/`index`/`load` sequence (same `--seed`) against `--solr-version 8.11.2` and `9.6`, then `searchlab compare v8.json v9.json --html regression.html`.

**Node failure under load:** start a long `load` run, `chaos pause solr2` mid-run, and read the story in the report timeline (p99 spike, error dots) plus `metrics --watch 5`.

## Tests

```
pip install pytest pytest-aiohttp
pytest -q
```

The suite covers data-generation determinism and shape, compose rendering, query template substitution, open-loop rate accuracy, drop accounting under saturation, ramping, and report generation — everything that doesn't require a Docker daemon.

## Roadmap

- Full k8s lifecycle management (the `k8s` command exports manifests today)
- Live GC panel in the dashboard
