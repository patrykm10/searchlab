# searchlab

Disposable search clusters — **SolrCloud, Elasticsearch, or OpenSearch** — with synthetic data of controllable shape, open-loop load tests, and a control panel that drives the whole thing from a browser. Built for two jobs: **learning search-engine internals** and **reproducing production performance issues** on demand.

Two ways in. The CLI, for scripting, CI gates and sweeps:

```
pip install -e .
searchlab quickstart              # up + collection + gen + index + load
```

…or the control panel, where you can ramp load, tune a live cluster, force merges, build queries and watch a failure unfold without touching a terminal:

```
searchlab up --engine opensearch --nodes 2
searchlab dashboard               # http://localhost:8990
```

Step by step, when you want control over each stage:

```
searchlab doctor            # preflight: docker, ports, disk
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

One CLI, one control panel, one report format. Everything downstream — `gen`, `index`, `load` (with `queries/es-default.yaml` DSL templates and `{RAND_WORD}` substitution anywhere in the body), `chaos`, `metrics`, `metrics-diff`, `compare`, `dashboard`, `quickstart` — works across all three engines; the cluster remembers its engine in `.searchlab/spec.json`.

The control panel adapts to whichever engine is running: live tuning knobs, segment detail, shard topology, splitting, semantic search and the query builder all have working equivalents on both sides, and where the two engines genuinely differ the UI says so rather than relabelling a Solr control (see [Solr and OpenSearch are not the same shape](#solr-and-opensearch-are-not-the-same-shape)).

Still Solr-only: `--monitoring` (the Prometheus stack), the `explain` CLI command, and server-side p99/rate on the dashboard — ES/OS node stats expose totals rather than percentiles, so heap, GC, caches, docs, thread pools, circuit breakers and merges all stream normally but the p99 trace comes from the client side. (The dashboard's **explain** checkbox does work on ES/OS, via the Profile API.)

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
| `scenario list/show/run` | Named reproductions — a data shape, a query mix, faults, and what to watch |
| `sweep sweep.yaml` | One workload x a config matrix, fresh cluster per cell, comparison table |
| `dashboard` | The control panel: drive the cluster, tune it live, build queries, read the incident timeline (`--demo` to preview) |
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

## Control panel

```
searchlab dashboard            # http://localhost:8990, drives the live cluster
searchlab dashboard --demo     # synthesized signals, preview without a cluster
```

A single self-contained page (no CDN, no build step) that both *shows* the cluster and *drives* it. Everything the CLI can do to a running cluster is a button here — the point being that you can demonstrate a merge storm or a commit-visibility surprise to someone who has never opened a terminal.

**Drive it:** ramp RPS live with a slider while a load test runs, index N documents of chosen complexity, force a commit or a merge, expunge deletes, reload, purge, create and delete collections, add and remove replicas by type, split a shard.

**Tune it while it runs:** knobs for soft/hard commit interval, filter and result cache size, RAM buffer, merge policy (segments per tier, max merged segment, deletes allowed), and merge scheduler threads. Turning one writes through the Config API on Solr, or index settings on ES/OS — no restart, no editing `solrconfig.xml`, and each knob links to the endpoint that proves its live value.

**Read it in plain language:** an insights panel that says *why* something is wrong rather than only that it is — "heap above 80% on solr2, which is why p99 is climbing" — with the alert history foldable so it stops disappearing before you finish reading.

**Incident timeline:** the differentiator. Rather than a wall of independent alerts, it links events into a causal chain across a time window — GC pause → ZooKeeper session lost → replica down → thread pool saturated → queries failing — so a drop gets a story instead of a metric. It distinguishes server-rejected from client-dropped, which matters: a load test can report tens of thousands of client drops while the server reports zero errors, and the difference tells you whether the cluster refused the work or was merely slow.

**Look inside Lucene:** per-shard segment detail — sizes, deleted-document share, and where each segment came from (a flush or a merge on Solr; committed and searchable state on ES/OS, which is a different and equally instructive fact), plus maxDoc, searchers opened, warmup time and sort statistics.

**Live log panel:** the engine's own logs, tailed into the page and lexed, so you can watch what the cluster says about itself as you press the buttons.

**Live load-test streaming:** while `searchlab load` runs, it writes rolling stats to `.searchlab/live-load.json` and the top panel comes alive automatically — client-observed p50/p99 traces, target vs achieved RPS, progress, errors, dropped. The client-side p99 next to the server-side `/select` p99 is the whole story of a saturation event on one screen: queueing shows up in the client trace before the server metric moves.

Under all that, the original strip-chart recorder on millimeter graph paper: pen traces for p99 latency (red), per-node heap sawtooth (blue), and query rate (green), with hover readouts and a selectable window.

## Query builder

The exploring half, next to the load testing. Build one query from menus and watch it run — then hand the same query to the load generator with **Run this as a load test**, so the thing you just tuned becomes the workload instead of the built-in mix.

On ES/OS the builder speaks the query DSL rather than Solr's params, and **renders the JSON body live as you build it** — every control is visibly a key in the request:

- **Query types** — `query_string`, `multi_match`, `match`, `match_phrase`, `term`, and `knn`, each carrying the parameters it actually accepts: multi_match's `type` (best_fields / most_fields / cross_fields / phrase / phrase_prefix / bool_prefix), `operator`, `minimum_should_match`, `fuzziness`, `tie_breaker`, `slop`, plus `from`, `highlight`, `min_score` and `track_total_hits`.
- **Conditions** — rows of dropdowns, each one a clause in `bool`. `is` / `is any of` / `contains` / `contains phrase` / `starts with` / `matches pattern` / `between` / `has any value` become `term` / `terms` / `match` / `match_phrase` / `prefix` / `wildcard` / `range` / `exists`. The **occurrence** picker is the point: `must` and `should` are scored, `filter` and `must_not` are not — the same condition in three positions gives three different answers, and you can watch the hit count and the scores move as you switch.
- **Aggregations** — bucket types (`terms`, `range`, `histogram`, `date_histogram`) that group documents and can hold a metric inside them, and metric types (`stats`, `avg`, `min`, `max`, `sum`, `value_count`, `cardinality`, `percentiles`) that compute one number. Average price per category is two dropdowns.
- **Sort and `_source`** — pickers rather than typed strings, offering only fields that can actually be sorted, because sorting on an analysed text field fails exactly the way aggregating on one does.

Parameters are gated to the clauses that accept them, so a control that is hidden is also absent from the request; where the engine would reject a combination outright — fuzziness on a phrase type — it is refused up front with the reason instead of arriving as a shard failure. Faceted buckets stay clickable to drill in, and a raw filter box remains as the escape hatch for what the menus cannot say.

Solr keeps its own parsers, wording, and text boxes.

## Semantic search — vectors as a relevance problem

The other use of the same technology, and it is worth keeping the two apart:
this half asks "did it return the right documents", which needs real text and a
judgement about what *right* means. Synthetic vectors cannot answer it, and the
[ANN tuning](#ann-tuning--vectors-as-a-performance-problem) loop below cannot either — that one measures speed and recall against
brute force, not whether an answer was any good. Confusing the two is the fastest
way to conclude that semantic search is either magic or broken.

**From the control panel**, for an index you already have: load an embedding model (MiniLM, BGE small/base, or Nomic — `pip install 'searchlab[embed]'`), pick a text field, and press **Embed documents**. It reads every document back, embeds the field, and writes the vector on — which is also exactly what a real re-embedding migration looks like, so it is worth watching and timing. After that, **semantic** mode in the query builder searches by meaning, next to the lexical query you just built.

Two things it tells you rather than hides. Re-embedding with a differently sized model is refused up front, because a mapped vector dimension cannot be changed. And on OpenSearch, an index that was not built for vectors has to be closed, reconfigured and reopened to enable `index.knn` — briefly unavailable, which is reported because on a real cluster it is an outage to plan for. (Lab-created indexes already set it, so they skip that.)

The model lives in the dashboard process, so restarting the server drops it and the **Embed documents** button disables itself until one is loaded again.

## Solr and OpenSearch are not the same shape

Most of the lab is one idea pointed at two APIs. A few things genuinely differ, and the UI names the difference rather than hiding it — which is most of the value if you are learning the second engine:

| | Solr | OpenSearch / Elasticsearch |
|---|---|---|
| Segment provenance | flush or merge | `committed` and `searchable`, which move independently |
| Replica placement | add one at a time, by type | set `number_of_replicas`; the cluster decides where copies live |
| Replica identity | named replicas | no names — a shard has a primary and N copies |
| Splitting | one shard, in place, writes keep flowing | the whole index into a **new** index; the source goes read-only first and stays that way |
| Hard commit | one call | `_refresh` **and** `_flush` — refresh alone is a soft commit |
| Adding vectors later | just add the field | `index.knn` is static, so the index must be closed, reconfigured and reopened |
| Deep pagination | cursorMark, stateless | the scroll API pins a server-side view that must be released |

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

## ANN tuning — vectors as a performance problem

This half asks "how fast, and how wrong?" The vectors are meaningless on
purpose: HNSW does not care what a vector means, only how the vectors are
distributed, so synthetic ones are the right tool and real embeddings would
only slow the loop down. If your question is instead "did it return the right
documents", you want [semantic search](#semantic-search--vectors-as-a-relevance-problem)
— different question, different data, further up this page.

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

The pathologies people talk about, as things the tool does rather than
instructions you follow:

```
searchlab scenario list
searchlab scenario show facet-pressure     # what it does, and what to look at
searchlab scenario run facet-pressure      # --dry-run to see the plan first
```

| Scenario | What it reproduces |
|---|---|
| `facet-pressure` | Faceting a near-unique field: cost scales with distinct values, not hits |
| `merge-storm` | Merges landing on the disk your queries are using — the p99 sawtooth |
| `deep-paging` | The offset cliff, and why page 1 tells you nothing about page 9,900 |
| `node-loss` | A frozen node (SIGSTOP) against a killed one — usually the freeze hurts more |

A scenario declares the data shape, the query mix, the cluster it wants, any
faults to inject, and — the part that makes it a lesson rather than a run —
**what to watch while it happens**, printed before the load starts and again
next to the report.

Two details that make them portable. Query mixes are per engine family, because
Solr speaks params and ES/OS speak a JSON body; a scenario that shipped only one
would fail as a run of uniform errors, which reads as a broken cluster. And
chaos steps name `node2` rather than `solr2`, resolved against whatever is
actually running — a drill written on Solr that silently does nothing on
OpenSearch is the failure this codebase keeps rediscovering.

Each mix deliberately runs the pathological query *next to* its cheap
equivalent, so the per-template breakdown in the report is the finding:
`facet_high_cardinality` against `facet_low_cardinality`, `page_deep` against
`page_shallow`, on identical documents at the same moment.

Scenarios are plain YAML in `scenarios/`; writing one for your own workload is a
text file away. They run on `drill`, so every scenario produces the same
annotated HTML report and accepts the same `--assert` gates.

**Version regression** is the one recipe that stays manual, because it spans two
clusters: run the identical `gen`/`index`/`load` sequence (same `--seed`) against
`--solr-version 8.11.2` and `9.6`, then
`searchlab compare v8.json v9.json --html regression.html`.

## Tests

```
pip install pytest pytest-aiohttp
pytest -q
```

The suite covers data-generation determinism and shape, compose rendering, query template substitution, open-loop rate accuracy, drop accounting under saturation, ramping, and report generation — everything that doesn't require a Docker daemon.

## Roadmap

- Full k8s lifecycle management (the `k8s` command exports manifests today)
- Live GC panel in the dashboard
