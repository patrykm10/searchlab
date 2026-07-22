"""Background action runner for the dashboard control panel.

Owns one asyncio event loop on a daemon thread; HTTP request threads submit
load/index coroutines to it via run_coroutine_threadsafe and mutate the
running load test through a shared LoadControl. One load test and one index
job at a time — this is a lab bench, not a scheduler.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from . import cluster as cl
from . import tuning
from .datagen import generate_to_file
from .indexer import index_file
from .loadtest import LoadControl, run_load

MAX_RPS = 2000.0
MAX_DOCS = 2_000_000
MAX_SHARDS = 8
MAX_REPLICAS = 4
COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

FALLBACK_PROFILE = (
    "fields:\n  id: {type: id}\n"
    "  title_t: {type: text, min_words: 3, max_words: 10}\n"
    "  body_t: {type: text, min_words: 50, max_words: 200}\n"
    "  category_s: {type: categorical, cardinality: 50, zipf: 1.1}\n"
    "  price_f: {type: float, min: 1, max: 5000}\n"
)

# Document-shape presets for the Add documents control. "standard" prefers
# the repo's richer profiles/default.yaml when running from a checkout.
DOC_PRESETS: dict[str, str | None] = {
    "simple": (
        "fields:\n  id: {type: id}\n"
        "  title_t: {type: text, min_words: 3, max_words: 8}\n"
        "  body_t: {type: text, min_words: 20, max_words: 60}\n"
        "  price_f: {type: float, min: 1, max: 500}\n"
        "  active_b: {type: bool, true_ratio: 0.9}\n"
    ),
    "standard": None,  # profiles/default.yaml when present, else FALLBACK_PROFILE
    "heavy-text": (
        "fields:\n  id: {type: id}\n"
        "  title_t: {type: text, min_words: 5, max_words: 15}\n"
        "  abstract_t: {type: text, min_words: 50, max_words: 150}\n"
        "  body_t: {type: text, min_words: 300, max_words: 800}\n"
        "  category_s: {type: categorical, cardinality: 30, zipf: 1.1}\n"
        "  published_dt: {type: date, days_back: 3650}\n"
    ),
    "high-cardinality": (
        "fields:\n  id: {type: id, uuid: true}\n"
        "  user_id_s: {type: keyword, length: 12}\n"
        "  session_id_s: {type: keyword, length: 16}\n"
        "  event_t: {type: text, min_words: 5, max_words: 15}\n"
        "  category_s: {type: categorical, cardinality: 2000}\n"
        "  tags_ss: {type: multivalued, min_values: 2, max_values: 10,\n"
        "            of: {type: categorical, cardinality: 5000, prefix: tag}}\n"
        "  ts_dt: {type: date, days_back: 90}\n"
        "  value_i: {type: int, min: 0, max: 1000000}\n"
    ),
}


class ActionRunner:
    def __init__(self, spec: cl.ClusterSpec):
        self.spec = spec
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self._lock = threading.Lock()
        self._load_future: Future | None = None
        self._load_meta: dict = {}
        self._control: LoadControl | None = None
        self._index_future: Future | None = None
        self._index_meta: dict = {}
        self._maint_busy = False
        self._replica_busy = False
        self._coll_busy = False
        self._model_loading = False
        self._model_name = "minilm"
        self._model_error: str | None = None
        self.last_action: dict | None = None

    # ------------------------------------------------------------- helpers --

    def _done(self, action: str, ok: bool, message: str) -> dict:
        self.last_action = {"action": action, "ok": ok, "message": message,
                            "ts": time.time()}
        return {"ok": ok, **({} if ok else {"error": message})}

    def _submit(self, action: str, coro, on_success) -> None:
        """Run `coro` on the background loop; fold the outcome into last_action."""

        async def guarded():
            try:
                res = await coro
            except (SystemExit, Exception) as e:  # SystemExit: CLI-era exits in deps
                self._done(action, False, str(e) or type(e).__name__)
            else:
                self._done(action, True, on_success(res))

        return asyncio.run_coroutine_threadsafe(guarded(), self.loop)

    @staticmethod
    def _running(future: Future | None) -> bool:
        return future is not None and not future.done()

    # ----------------------------------------------------------- load test --

    def start_load(self, collection: str, rps: float,
                   query: dict | None = None) -> dict:
        """Start a load test, either from the shipped templates or from a
        query built in the UI (`query` being its Solr params)."""
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if not 0 < rps <= MAX_RPS:
            return {"ok": False, "error": f"Rate must be between 1 and {MAX_RPS:.0f}."}
        templates = None
        if query:
            from .query import build_params

            try:
                params = build_params(query)
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            params.pop("wt", None)          # the engine sets its own
            templates = [{"name": "custom", "weight": 1, "params": params}]
        with self._lock:
            if self._running(self._load_future):
                return {"ok": False, "error": "A load test is already running."}
            self._control = LoadControl(rps=rps)
            self._load_meta = {"collection": collection, "started": time.time(),
                               "custom": bool(query)}
            queries = Path("queries/default.yaml")
            self._load_future = self._submit(
                "load",
                run_load(self.spec.base_url(), collection, rps, duration=86400.0,
                         queries_path=None if templates else
                         (queries if queries.exists() else None),
                         templates=templates,
                         live_file=cl.WORKDIR / "live-load.json",
                         engine=self.spec.engine, control=self._control),
                lambda res: f"Load test finished: {len(res.records)} requests sent.",
            )
        return {"ok": True}

    def set_rps(self, rps: float) -> dict:
        if not 0 < rps <= MAX_RPS:
            return {"ok": False, "error": f"Rate must be between 1 and {MAX_RPS:.0f}."}
        with self._lock:
            if not self._running(self._load_future) or self._control is None:
                return {"ok": False, "error": "No load test is running."}
            self._control.rps = rps
        return {"ok": True}

    def stop_load(self) -> dict:
        with self._lock:
            if not self._running(self._load_future) or self._control is None:
                return {"ok": False, "error": "No load test is running."}
            self._control.stop_requested = True
        return {"ok": True}

    # ------------------------------------------------------------ indexing --

    def index_docs(self, collection: str, count: int, preset: str = "standard") -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if not 0 < count <= MAX_DOCS:
            return {"ok": False, "error": f"Count must be between 1 and {MAX_DOCS:,}."}
        if preset not in DOC_PRESETS:
            return {"ok": False,
                    "error": f"Document shape must be one of {', '.join(DOC_PRESETS)}."}
        with self._lock:
            if self._running(self._index_future):
                return {"ok": False, "error": "An indexing job is already running."}
            cl.WORKDIR.mkdir(exist_ok=True)
            if preset == "standard" and Path("profiles/default.yaml").exists():
                profile = Path("profiles/default.yaml")
            else:
                profile = cl.WORKDIR / f"ui-profile-{preset}.yaml"
                profile.write_text(DOC_PRESETS[preset] or FALLBACK_PROFILE)
            data = cl.WORKDIR / "ui-data.jsonl"

            async def job():
                await asyncio.to_thread(generate_to_file, profile, count, data)
                return await index_file(self.spec.base_url(), collection, data,
                                        threads=4, engine=self.spec.engine)

            self._index_meta = {"collection": collection, "count": count}
            self._index_future = self._submit(
                "index", job(),
                lambda st: f"Indexing done: {st.summary()}",
            )
        return {"ok": True}

    # --------------------------------------------------------- maintenance --

    def commit(self, collection: str) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        try:
            cl.commit(self.spec, collection)
        except Exception as e:
            return self._done("commit", False, str(e))
        return self._done("commit", True, "Committed — recent documents are now searchable.")

    def _maintenance_job(self, collection: str, action: str, message: str,
                         fn) -> dict:
        """Run a slow collection-wide operation in the background.

        One at a time: expunging while a merge rewrites the same segments
        just makes both slower.
        """
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        with self._lock:
            if self._maint_busy:
                return {"ok": False, "error": "Another maintenance job is running."}
            self._maint_busy = True

        def job():
            try:
                # a job may return its own message when the useful detail
                # (a count, say) is only known once it has run
                result = fn()
                self._done(action, True,
                           result if isinstance(result, str) else message)
            except Exception as e:
                self._done(action, False, str(e))
            finally:
                self._maint_busy = False

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    def optimize(self, collection: str) -> dict:
        return self._maintenance_job(
            collection, "optimize", "Merge finished — index compacted.",
            lambda: cl.optimize(self.spec, collection))

    def expunge_deletes(self, collection: str) -> dict:
        return self._maintenance_job(
            collection, "expunge",
            "Expunge finished — space from deleted documents reclaimed.",
            lambda: cl.expunge_deletes(self.spec, collection))

    def reload_collection(self, collection: str) -> dict:
        return self._maintenance_job(
            collection, "reload",
            "Collection reloaded — config re-read and caches are cold.",
            lambda: cl.reload_collection(self.spec, collection))

    def delete_all_docs(self, collection: str) -> dict:
        load = self.state()["load"]
        if load["running"] and load["collection"] == collection:
            return {"ok": False, "error": "Stop the load test on this collection first."}
        if self._running(self._index_future) and \
                self._index_meta.get("collection") == collection:
            return {"ok": False, "error": "Wait for the indexing job to finish."}
        return self._maintenance_job(
            collection, "purge",
            f"All documents deleted from “{collection}” — the collection remains.",
            lambda: cl.delete_all_docs(self.spec, collection))

    def index_path(self, collection: str, path: str) -> dict:
        """Index a real file from disk (CSV/TSV/JSON/JSONL) by server path."""
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        src = Path(path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"No file at {src}"}
        with self._lock:
            if self._running(self._index_future):
                return {"ok": False, "error": "An indexing job is already running."}
            self._index_meta = {"collection": collection, "count": None,
                                "source": src.name}
            self._index_future = self._submit(
                "index",
                index_file(self.spec.base_url(), collection, src, threads=4,
                           engine=self.spec.engine),
                lambda st: f"Indexed {src.name}: {st.summary()}",
            )
        return {"ok": True}

    def preview_path(self, path: str) -> dict:
        """Inferred column mapping for a file, so the UI can show it first."""
        from .tabular import describe

        src = Path(path).expanduser()
        if not src.is_file():
            return {"ok": False, "error": f"No file at {src}"}
        try:
            return {"ok": True, **describe(src)}
        except Exception as e:
            return {"ok": False, "error": f"Could not read {src.name}: {e}"}

    # --------------------------------------------------------- embeddings --

    def model_state(self) -> dict:
        from . import embeddings as emb

        return {
            "available": emb.available(),
            "loading": self._model_loading,
            "loaded": emb.loaded_names(),
            "current": self._model_name,
            "dims": emb.MODELS.get(self._model_name, (None, None))[1],
            "models": {k: {"id": v[0], "dims": v[1]} for k, v in emb.MODELS.items()},
            "error": self._model_error,
        }

    def load_model(self, name: str) -> dict:
        """Load an embedding model. First use downloads weights, so this
        runs in the background and the UI polls model_state()."""
        from . import embeddings as emb

        if not emb.available():
            return {"ok": False, "error": emb.INSTALL_HINT}
        if name not in emb.MODELS:
            return {"ok": False, "error": f"Unknown model {name!r}."}
        with self._lock:
            if self._model_loading:
                return {"ok": False, "error": "A model is already loading."}
            self._model_loading = True
            self._model_error = None

        def job():
            try:
                emb.get(name)
                self._model_name = name
                self._done("model", True,
                           f"Embedding model “{name}” ready — "
                           f"{emb.MODELS[name][1]} dimensions.")
            except Exception as e:
                self._model_error = str(e)
                self._done("model", False, str(e))
            finally:
                self._model_loading = False

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    def embed_collection(self, collection: str, text_field: str,
                         vector_field: str = "vec") -> dict:
        """Re-index every document with an embedding of one text field.

        Solr can't compute vectors itself, so this reads the documents back
        out, embeds them here, and writes them again — which is exactly what
        a real re-embedding migration looks like.
        """
        from . import embeddings as emb

        if not collection or not text_field:
            return {"ok": False, "error": "Pick a collection and a text field."}
        if not emb.available():
            return {"ok": False, "error": emb.INSTALL_HINT}
        if self._model_name not in emb.loaded_names():
            return {"ok": False, "error": "Load an embedding model first."}

        model = emb.get(self._model_name)

        def job():
            from .vectorize import embed_existing_docs

            n = embed_existing_docs(self.spec, collection, model,
                                    text_field, vector_field)
            return f"Embedded {n:,} documents into “{vector_field}”."

        return self._maintenance_job(collection, "embed",
                                     "Embedding finished.", job)

    # -------------------------------------------------------------- query --

    def fields(self, collection: str) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "The query builder is Solr-only for now."}
        from .query import list_fields

        try:
            return {"ok": True, "fields": list_fields(self.spec, collection)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_query(self, collection: str, body: dict) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "The query builder is Solr-only for now."}
        from .query import run_query

        embedder = None
        if body.get("semantic"):
            from . import embeddings as emb

            if not emb.available():
                return {"ok": False, "error": emb.INSTALL_HINT}
            if self._model_name not in emb.loaded_names():
                return {"ok": False, "error": "Load an embedding model first."}
            embedder = emb.get(self._model_name)
        try:
            return run_query(self.spec, collection, body, embedder=embedder)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --------------------------------------------------------- collections --

    def create_collection(self, name: str, shards: int, replicas: int) -> dict:
        if not COLLECTION_NAME_RE.match(name or ""):
            return {"ok": False, "error": "Collection names use letters, digits, "
                                          "hyphens and underscores (max 64 chars)."}
        if not 1 <= shards <= MAX_SHARDS:
            return {"ok": False, "error": f"Shards must be between 1 and {MAX_SHARDS}."}
        if not 1 <= replicas <= MAX_REPLICAS:
            return {"ok": False, "error": f"Copies must be between 1 and {MAX_REPLICAS}."}
        with self._lock:
            if self._coll_busy:
                return {"ok": False, "error": "A collection change is already running."}
            self._coll_busy = True

        def job():
            try:
                cl.create_collection(self.spec, name, shards, replicas)
                self._done("create_collection", True,
                           f"Collection “{name}” created — {shards} shard(s), "
                           f"{replicas} cop{'y' if replicas == 1 else 'ies'} each.")
            except (SystemExit, Exception) as e:  # engines sys.exit on failure
                self._done("create_collection", False, str(e))
            finally:
                self._coll_busy = False

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    def delete_collection(self, name: str) -> dict:
        if not name:
            return {"ok": False, "error": "Pick a collection first."}
        load = self.state()["load"]
        if load["running"] and load["collection"] == name:
            return {"ok": False, "error": "Stop the load test on this collection first."}
        if self._running(self._index_future) and self._index_meta.get("collection") == name:
            return {"ok": False, "error": "Wait for the indexing job on this collection to finish."}
        with self._lock:
            if self._coll_busy:
                return {"ok": False, "error": "A collection change is already running."}
            self._coll_busy = True

        def job():
            try:
                cl.delete_collection(self.spec, name)
                self._done("delete_collection", True, f"Collection “{name}” deleted.")
            except (SystemExit, Exception) as e:
                self._done("delete_collection", False, str(e))
            finally:
                self._coll_busy = False

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------ replicas --

    def topology(self, collection: str) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Replica management is Solr-only for now."}
        try:
            detail = cl.collection_detail(self.spec, collection)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "busy": self._replica_busy, **detail}

    def _replica_job(self, action: str, message: str, fn) -> dict:
        with self._lock:
            if self._replica_busy:
                return {"ok": False, "error": "A replica change is already running."}
            self._replica_busy = True

        def job():
            try:
                fn()
                self._done(action, True, message)
            except Exception as e:
                self._done(action, False, str(e))
            finally:
                self._replica_busy = False

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    def add_replica(self, collection: str, shard: str, replica_type: str) -> dict:
        if not collection or not shard:
            return {"ok": False, "error": "Pick a collection and shard first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Replica management is Solr-only for now."}
        if replica_type not in cl.REPLICA_TYPES:
            return {"ok": False,
                    "error": f"Replica type must be one of {', '.join(cl.REPLICA_TYPES)}."}
        return self._replica_job(
            "add_replica",
            f"Added a {replica_type} replica to {shard} — it will sync and go active shortly.",
            lambda: cl.add_replica(self.spec, collection, shard, replica_type))

    def split_shard(self, collection: str, shard: str) -> dict:
        if not collection or not shard:
            return {"ok": False, "error": "Pick a collection and shard first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Shard splitting is Solr-only for now."}
        return self._replica_job(
            "split_shard",
            f"Split {shard} into two sub-shards — the original is now inactive.",
            lambda: cl.split_shard(self.spec, collection, shard))

    def remove_replica(self, collection: str, shard: str, replica: str) -> dict:
        if not collection or not shard or not replica:
            return {"ok": False, "error": "Pick a replica to remove first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Replica management is Solr-only for now."}
        return self._replica_job(
            "remove_replica",
            f"Removed replica {replica} from {shard}.",
            lambda: cl.delete_replica(self.spec, collection, shard, replica))

    # -------------------------------------------------------------- tuning --

    def read_tuning(self, collection: str) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Tuning is Solr-only for now."}
        try:
            state = tuning.tuning_state(self.spec, collection)
        except Exception:
            # A collection just created can 404 on /config for a moment while
            # its core finishes coming up — transient, so stay vague and let
            # the UI retry rather than surfacing a raw HTTP exception.
            return {"ok": False, "error": "Settings aren't available yet — retrying…",
                    "transient": True}
        return {"ok": True, **state}

    def tune(self, collection: str, name: str, value: float) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Tuning is Solr-only for now."}
        try:
            tuning.apply_tuning(self.spec, collection, name, value)
        except Exception as e:
            return self._done("tune", False, str(e))
        knob = tuning.KNOBS[name]
        return self._done(
            "tune", True,
            f"{knob['label']} set to {value:g} {knob['unit']} — live within a few seconds.")

    # ----------------------------------------------------------------- state --

    def state(self) -> dict:
        load_running = self._running(self._load_future)
        return {
            "load": {
                "running": load_running,
                "collection": self._load_meta.get("collection") if load_running else None,
                "target_rps": self._control.rps if load_running and self._control else None,
                "elapsed_s": round(time.time() - self._load_meta["started"], 1)
                if load_running and "started" in self._load_meta else None,
            },
            "index": {
                "running": self._running(self._index_future),
                "collection": self._index_meta.get("collection"),
                "count": self._index_meta.get("count"),
            },
            "maintenance": self._maint_busy,
            "collection_busy": self._coll_busy,
            "last_action": self.last_action,
        }
