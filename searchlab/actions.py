"""Background action runner for the dashboard control panel.

Owns one asyncio event loop on a daemon thread; HTTP request threads submit
load/index coroutines to it via run_coroutine_threadsafe and mutate the
running load test through a shared LoadControl. One load test and one index
job at a time — this is a lab bench, not a scheduler.
"""

from __future__ import annotations

import asyncio
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

FALLBACK_PROFILE = (
    "fields:\n  id: {type: id}\n"
    "  title_t: {type: text, min_words: 3, max_words: 10}\n"
    "  body_t: {type: text, min_words: 50, max_words: 200}\n"
    "  category_s: {type: categorical, cardinality: 50, zipf: 1.1}\n"
    "  price_f: {type: float, min: 1, max: 5000}\n"
)


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
        self._optimizing = False
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

    def start_load(self, collection: str, rps: float) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if not 0 < rps <= MAX_RPS:
            return {"ok": False, "error": f"Rate must be between 1 and {MAX_RPS:.0f}."}
        with self._lock:
            if self._running(self._load_future):
                return {"ok": False, "error": "A load test is already running."}
            self._control = LoadControl(rps=rps)
            self._load_meta = {"collection": collection, "started": time.time()}
            queries = Path("queries/default.yaml")
            self._load_future = self._submit(
                "load",
                run_load(self.spec.base_url(), collection, rps, duration=86400.0,
                         queries_path=queries if queries.exists() else None,
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

    def index_docs(self, collection: str, count: int) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if not 0 < count <= MAX_DOCS:
            return {"ok": False, "error": f"Count must be between 1 and {MAX_DOCS:,}."}
        with self._lock:
            if self._running(self._index_future):
                return {"ok": False, "error": "An indexing job is already running."}
            profile = Path("profiles/default.yaml")
            if not profile.exists():  # installed without the repo checkout
                cl.WORKDIR.mkdir(exist_ok=True)
                profile = cl.WORKDIR / "default-profile.yaml"
                profile.write_text(FALLBACK_PROFILE)
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

    def optimize(self, collection: str) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        with self._lock:
            if self._optimizing:
                return {"ok": False, "error": "A merge is already running."}
            self._optimizing = True

        def job():
            try:
                cl.optimize(self.spec, collection)
                self._done("optimize", True, "Merge finished — index compacted.")
            except Exception as e:
                self._done("optimize", False, str(e))
            finally:
                self._optimizing = False

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    # -------------------------------------------------------------- tuning --

    def read_tuning(self, collection: str) -> dict:
        if not collection:
            return {"ok": False, "error": "Pick a collection first."}
        if self.spec.engine != "solr":
            return {"ok": False, "error": "Tuning is Solr-only for now."}
        try:
            state = tuning.tuning_state(self.spec, collection)
        except Exception as e:
            return {"ok": False, "error": str(e)}
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
            "optimizing": self._optimizing,
            "last_action": self.last_action,
        }
