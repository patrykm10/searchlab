# Changelog

## 0.14.0 — interactive learning
- `learn`: an interactive lesson engine that teaches against the LIVE
  cluster — its `wait` step tells you to go do something real (kill a node
  in another terminal) and polls actual cluster state until it happens.
  Step types: say / pause / run / http (with expected-state checks) /
  wait / ask (scored multiple choice with explanations). Three built-in
  lessons: cluster-anatomy, leader-election (you cause one and the lesson
  notices), commits-and-visibility (reproduces the classic "where's my
  document" surprise, then resolves it)
- `explain`: runs a Solr query with debug=true and translates the output —
  what your query became after analysis, where the time went per component
  (flagging any component eating >50%), and the top document's score tree
  pruned to the part humans can read
- Packaging fix: dashboard.html and lessons/*.yaml added to package data
  (editable installs masked the omission; wheels would have shipped broken)


## 0.13.0
- `recall`: measure approximate-kNN recall@k against exact ground truth
  computed locally over the generated dataset (numpy, optional extra:
  `pip install searchlab[recall]`) — query vectors drawn from the same
  clustered distribution with a different seed (in-distribution but unseen),
  latency reported alongside recall, and `--candidates 20,50,100,500` sweeps
  the ES num_candidates knob to produce the recall/latency tradeoff curve;
  warns and hints at HNSW build knobs when recall < 0.9


## 0.12.0 — vector search
- `vector` field type in data profiles: clustered unit vectors (centroids +
  gaussian noise — the shape real embeddings have; uniform random is
  pathological for ANN), with `dims`, `clusters`, `cluster_std`,
  `similarity`, seeded and deterministic
- Schema across engines: Solr `DenseVectorField` (fieldType auto-created
  first, HNSW knobs `hnswMaxConnections`/`hnswBeamWidth` via `solr:` block),
  ES `dense_vector`, OS `knn_vector` (correct `space_type` mapping);
  OpenSearch lab indexes are created with `index.knn: true` (static setting)
- `{RAND_VECTOR:dims}` in query templates: becomes a real JSON array when it
  is the whole value (ES/OS bodies), bracketed text when embedded in a string
  (Solr `{!knn}` syntax) — fresh normalized vector per request
- Shipped: `profiles/vectors.yaml`, `queries/vector-solr.yaml`,
  `queries/vector-es.yaml`, `queries/vector-os.yaml` (incl. filtered kNN and
  deep-topK templates — the two classic latency levers)


## 0.11.0
- `sweep`: run one seeded workload across a matrix of cluster configs (heap,
  GC flags, versions, node counts, engines) — fresh cluster per cell so cells
  can't contaminate each other, dataset generated once and reused — producing
  a comparison matrix (JSON + HTML) with best-per-metric highlighting (ties
  count as best) and indexing throughput per cell; teardown is guaranteed
  even when a cell fails
- `examples/heap-sweep.yaml`: 3 heap sizes x 2 collectors


## 0.10.0
- `k8s`: export Kubernetes operator manifests mirroring the lab spec —
  SolrCloud CR (Apache Solr Operator), Elasticsearch CR (ECK, TLS/auth
  disabled for lab parity), OpenSearchCluster CR (OpenSearch Operator) —
  with operator install hints in the header; defaults from the running
  cluster, overridable by flags, works with no cluster at all
- `gclog --html`: per-node GC pause timeline (square-root scale keeps young
  pauses visible next to Full GCs; Fulls get annotated markers) plus the
  text summary in one self-contained page
- `gclog` now finds ES/OS logs (`gc.log*`) alongside Solr's `solr_gc.log*`,
  and the ES/OS compose template already mounts the right directory with
  `--gc-logs` — GC analysis now works on all three engines


## 0.9.0
- Assertion gates: `--assert "p99_ms<50"` (repeatable) on `load`, `replay`,
  and `drill` (also an `assert:` list in drill YAML) — exits 1 on failure,
  turning any run into a CI regression gate; assertions are validated before
  the run starts
- Human-friendly values: durations accept `90s`/`2m`/`1h30m` (load, replay
  ramp/duration), counts accept `10k`/`1.5m` (gen, quickstart)
- Friendlier failure UX: an all-requests-failed run prints a
  cluster-health hint
- Housekeeping: ruff lint config + CI lint job, dead code removed
  (`cluster_status`, stray imports), smoke job now exercises the gates


## 0.8.0
- `drill`: orchestrated failure drills from one YAML — metrics snapshot,
  open-loop load with timed chaos injected mid-flight, snapshot again, and a
  single self-contained HTML report with fault injections drawn as annotated
  markers on the latency timeline, plus the latency histogram and the
  before/after metrics diff
- Drill validation: chaos steps must fall inside the load window
- `examples/drill-node-loss.yaml`: freeze/thaw/kill/restore drill


## 0.7.0
- `schema` now supports Elasticsearch/OpenSearch: index mappings derived from
  data profiles, per-field `es:` overrides (e.g. `doc_values: false`),
  `--dry-run --engine es` works without a cluster
- `replay` now parses ES/OS search slow logs (classic bracketed and JSON-lines
  formats); replays the recorded `source` bodies against `_search`
- CI workflow: unit suite on Python 3.10–3.12 plus a real-Solr `quickstart`
  smoke job in Docker
- Apache-2.0 LICENSE, changelog

## 0.6.0
- Engine abstraction: Solr, Elasticsearch, and OpenSearch behind one CLI
  (`up --engine opensearch`); spec remembers its engine
- ES/OS: compose template (single-node and multi-node discovery, security
  disabled for lab use), `_bulk` NDJSON indexing with per-item error checks,
  query-DSL templates (`body:`) with recursive placeholder substitution,
  `_nodes/stats` normalized into the common metrics shape — dashboard,
  `metrics`, `metrics-diff`, `chaos`, `status`, `quickstart` work unchanged

## 0.5.0
- `replay`: replay real Solr request logs at original, scaled (`--speed`), or
  uniform (`--rps`) pacing with `--loop`; URL decoding, repeated `fq` keys
- `gclog`: unified-logging GC pause analysis — tail percentiles per pause
  type, throughput lost to GC, Full GC detection
- `doctor`: preflight checks (docker, compose, ports, disk, leftover state)

## 0.4.0
- Live load-test streaming: `load` writes rolling stats; dashboard's top panel
  shows client-observed p50/p99, target vs achieved RPS, progress, drops
- `schema` (Solr): explicit fields from data profiles, `solr:` overrides
- `quickstart`: up + collection + gen + index + load in one command
- Reports gained p99.9 and a log-bucketed latency histogram

## 0.3.0
- `dashboard`: strip-chart recorder UI (millimeter graph paper, pen traces)
  for p99, heap sawtooth, query rate, caches, GC, merges; `--demo` mode
- `chaos run scenario.yaml`: timed fault drills
- `metrics-diff`: before/after deltas between snapshots

## 0.2.0
- `chaos kill/pause/unpause/start/restart`: node fault injection
- `metrics`: distilled per-node snapshot (heap, GC, caches, merges, p99)
- `compare`: A/B diff of load reports; self-contained HTML reports with SVG
- pytest suite

## 0.1.0
- SolrCloud provisioning via docker-compose (version, heap, GC_TUNE, optional
  Prometheus/Grafana, GC log mounts)
- Synthetic data from YAML profiles (cardinality, Zipf skew, text length,
  multivalued); seeded reproducibility
- Async bulk indexer; open-loop load generator (token-bucket schedule, no
  coordinated omission) with ramping, weighted query templates, mixed
  query+index workloads, JSON reports with timeline
