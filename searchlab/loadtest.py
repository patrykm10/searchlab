"""Open-loop load generation against a Solr collection.

Requests are fired on a fixed wall-clock schedule derived from the target RPS,
regardless of whether earlier requests have completed. This avoids coordinated
omission: a closed-loop client slows down when the server slows down, which
silently hides the worst latencies — exactly the ones you care about when
reproducing an incident.

Workloads mix weighted query templates and optional concurrent index load.
Latency is recorded per request and reported as percentiles over the full run
plus a per-interval timeline.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from .datagen import FieldGen, load_profile
from .engines import get_engine

_DEFAULT_QUERIES = [
    {"name": "match_all", "weight": 1, "params": {"q": "*:*", "rows": 10}},
]


@dataclass
class RequestRecord:
    scheduled: float
    latency_ms: float
    status: int
    template: str
    ok: bool


@dataclass
class LoadControl:
    """Mutable knobs read by run_load each scheduler tick. Assignments from
    another thread are safe: float/bool stores are atomic under the GIL."""

    rps: float = 0.0
    stop_requested: bool = False


@dataclass
class LoadResult:
    records: list[RequestRecord] = field(default_factory=list)
    dropped: int = 0  # scheduled but never sent (client saturated)
    duration: float = 0.0
    target_rps: float = 0.0

    def percentile(self, p: float, subset: list[RequestRecord] | None = None) -> float:
        recs = subset if subset is not None else self.records
        lats = sorted(r.latency_ms for r in recs)
        if not lats:
            return float("nan")
        k = min(int(len(lats) * p / 100), len(lats) - 1)
        return lats[k]

    def summary(self) -> str:
        n = len(self.records)
        errs = sum(1 for r in self.records if not r.ok)
        achieved = n / self.duration if self.duration else 0
        lines = [
            f"requests:     {n} sent, {errs} errors, {self.dropped} dropped",
            f"target rps:   {self.target_rps:.1f}",
            f"achieved rps: {achieved:.1f}",
            f"latency p50:  {self.percentile(50):.1f} ms",
            f"latency p90:  {self.percentile(90):.1f} ms",
            f"latency p99:  {self.percentile(99):.1f} ms",
            f"latency max:  {max((r.latency_ms for r in self.records), default=float('nan')):.1f} ms",
        ]
        by_template: dict[str, list[RequestRecord]] = {}
        for r in self.records:
            by_template.setdefault(r.template, []).append(r)
        if len(by_template) > 1:
            lines.append("per template:")
            for name, recs in sorted(by_template.items()):
                lines.append(
                    f"  {name}: n={len(recs)} p50={self.percentile(50, recs):.1f}ms "
                    f"p99={self.percentile(99, recs):.1f}ms"
                )
        return "\n".join(lines)

    def timeline(self, bucket_s: float = 5.0) -> list[dict]:
        if not self.records:
            return []
        t0 = min(r.scheduled for r in self.records)
        buckets: dict[int, list[RequestRecord]] = {}
        for r in self.records:
            buckets.setdefault(int((r.scheduled - t0) / bucket_s), []).append(r)
        out = []
        for b in sorted(buckets):
            recs = buckets[b]
            out.append(
                {
                    "t": round(b * bucket_s, 1),
                    "rps": round(len(recs) / bucket_s, 1),
                    "p50_ms": round(self.percentile(50, recs), 1),
                    "p99_ms": round(self.percentile(99, recs), 1),
                    "errors": sum(1 for r in recs if not r.ok),
                }
            )
        return out


class QueryPicker:
    """Weighted random choice over query templates with term randomization.

    Template params may contain `{RAND_WORD}`, `{RAND_INT:lo:hi}` placeholders,
    substituted per request so caches don't fake your numbers.
    """

    def __init__(self, templates: list[dict], rng: random.Random, words: list[str]):
        self.templates = templates
        self.weights = [float(t.get("weight", 1)) for t in templates]
        self.rng = rng
        self.words = words

    _VEC_EXACT = re.compile(r"^\{RAND_VECTOR:(\d+)\}$")

    def _rand_vector(self, dims: int) -> list[float]:
        v = [self.rng.gauss(0, 1) for _ in range(dims)]
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [round(x / norm, 4) for x in v]

    def _sub(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        m = self._VEC_EXACT.match(value)
        if m:  # the placeholder IS the value: substitute a real list (ES/OS body)
            return self._rand_vector(int(m.group(1)))
        while "{RAND_VECTOR:" in value:  # embedded in a string: Solr {!knn} syntax
            start = value.index("{RAND_VECTOR:")
            end = value.index("}", start)
            dims = int(value[start + 13:end])
            vec_txt = "[" + ", ".join(str(x) for x in self._rand_vector(dims)) + "]"
            value = value[:start] + vec_txt + value[end + 1:]
        while "{RAND_WORD}" in value:
            value = value.replace("{RAND_WORD}", self.rng.choice(self.words), 1)
        while "{RAND_INT:" in value:
            start = value.index("{RAND_INT:")
            end = value.index("}", start)
            _, lo, hi = value[start + 1 : end].split(":")
            value = value[:start] + str(self.rng.randint(int(lo), int(hi))) + value[end + 1 :]
        return value

    def _sub_any(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._sub_any(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._sub_any(v) for v in value]
        return self._sub(value)

    def pick_template(self) -> dict:
        """Weighted pick with placeholders substituted through params or body."""
        t = self.rng.choices(self.templates, weights=self.weights, k=1)[0]
        return {k: self._sub_any(v) if k in ("params", "body") else v for k, v in t.items()}

    def pick(self) -> tuple[str, dict]:
        t = self.pick_template()
        params = dict(t.get("params", {}))
        params.setdefault("wt", "json")
        return t.get("name", "unnamed"), params


def load_queries(path: str | Path | None) -> list[dict]:
    if path is None:
        return _DEFAULT_QUERIES
    data = yaml.safe_load(Path(path).read_text())
    templates = data.get("queries", data) if isinstance(data, dict) else data
    if not isinstance(templates, list) or not templates:
        raise SystemExit(f"searchlab: no query templates found in {path}")
    return templates


async def run_load(
    base_url: str,
    collection: str,
    rps: float,
    duration: float,
    ramp: float = 0.0,
    queries_path: str | Path | None = None,
    index_rps: float = 0.0,
    index_profile: str | Path | None = None,
    max_in_flight: int = 500,
    seed: int | None = None,
    timeout: float = 30.0,
    words: list[str] | None = None,
    live_file: str | Path | None = None,
    engine: str = "solr",
    control: LoadControl | None = None,
) -> LoadResult:
    """Fire queries at `rps` for `duration` seconds, ramping linearly over `ramp`.

    If `index_rps` > 0, a concurrent stream of single-doc updates (generated
    from `index_profile`) runs on its own fixed schedule — mixed read/write
    load is where merge- and cache-related pathologies actually show up.

    A `control` object makes the run steerable while it executes: its `rps`
    replaces the static target each scheduler tick, and `stop_requested`
    ends the run before `duration`.
    """
    from .datagen import _WORDS  # embedded corpus as default term source

    eng = get_engine(engine)
    rng = random.Random(seed)
    templates = load_queries(queries_path) if queries_path else eng.default_queries()
    picker = QueryPicker(templates, rng, words or list(_WORDS))

    index_gens = None
    if index_rps > 0:
        profile = load_profile(index_profile) if index_profile else {"fields": {"id": {"type": "id", "uuid": True}, "body_t": {"type": "text"}}}
        index_gens = [FieldGen(name, cfg or {}, rng) for name, cfg in profile["fields"].items()]

    result = LoadResult(target_rps=rps)
    in_flight = 0  # plain counter: single event loop, no await between check and increment
    tasks: set[asyncio.Task] = set()

    async def fire_query(client: httpx.AsyncClient, scheduled: float) -> None:
        t = picker.pick_template()
        name = t.get("name", "unnamed")
        start = time.perf_counter()
        try:
            r = await client.request(**eng.search_request(base_url, collection, t))
            ok = r.status_code == 200
            status = r.status_code
        except httpx.HTTPError:
            ok, status = False, 0
        result.records.append(
            RequestRecord(scheduled, (time.perf_counter() - start) * 1000, status, name, ok)
        )
        nonlocal in_flight
        in_flight -= 1

    async def fire_index(client: httpx.AsyncClient, scheduled: float, seq: int) -> None:
        doc = {g.name: g.value(seq) for g in index_gens}
        start = time.perf_counter()
        try:
            r = await client.request(**eng.bulk_request(base_url, collection, [doc], 5000))
            ok = r.status_code == 200
            status = r.status_code
        except httpx.HTTPError:
            ok, status = False, 0
        result.records.append(
            RequestRecord(scheduled, (time.perf_counter() - start) * 1000, status, "_index", ok)
        )
        nonlocal in_flight
        in_flight -= 1

    def _fire(coro) -> None:
        """Launch a request without waiting on it; drop if in-flight cap is hit."""
        nonlocal in_flight
        if in_flight >= max_in_flight:
            result.dropped += 1  # open loop: never queue behind slow responses
            coro.close()
            return
        in_flight += 1
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=max_in_flight)) as client:
        t0 = time.perf_counter()
        prev = 0.0
        q_tokens = 1.0  # start with one token so the first request fires immediately
        i_tokens = 1.0 if index_rps > 0 else 0.0
        idx_seq = 0
        tick = min(0.001, 0.25 / max(rps + index_rps, 1))

        next_live = 0.0
        while True:
            now = time.perf_counter() - t0
            if now >= duration:
                break
            if control is not None and control.stop_requested:
                break
            eff_rps = control.rps if control is not None else rps
            result.target_rps = eff_rps

            if live_file and now >= next_live:
                next_live = now + 1.0
                _write_live(live_file, result, now, duration)

            # Token bucket integrated over the ramp: tokens accrue at the
            # *current* effective rate, so ramping works correctly and bursts
            # after event-loop hiccups are naturally bounded (cap at 1s worth).
            frac = min(now / ramp, 1.0) if ramp > 0 else 1.0
            dt = now - prev
            prev = now
            q_tokens = min(q_tokens + eff_rps * frac * dt, max(eff_rps * frac, 1.0))
            if index_rps > 0:
                i_tokens = min(i_tokens + index_rps * frac * dt, max(index_rps * frac, 1.0))

            while q_tokens >= 1.0:
                q_tokens -= 1.0
                _fire(fire_query(client, now))

            while i_tokens >= 1.0:
                i_tokens -= 1.0
                _fire(fire_index(client, now, idx_seq))
                idx_seq += 1

            await asyncio.sleep(tick)

        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        result.duration = time.perf_counter() - t0

    return result


def _write_live(path: str | Path, result: LoadResult, elapsed: float, duration: float) -> None:
    """Rolling 5s-window stats for the dashboard; best-effort, never fatal."""
    cutoff = elapsed - 5.0
    recent = [r for r in result.records if r.scheduled >= cutoff]
    payload = {
        "ts": time.time(),
        "elapsed_s": round(elapsed, 1),
        "duration_s": duration,
        "target_rps": result.target_rps,
        "recent_rps": round(len(recent) / min(5.0, max(elapsed, 0.1)), 1),
        "recent_p50_ms": round(result.percentile(50, recent), 1) if recent else None,
        "recent_p99_ms": round(result.percentile(99, recent), 1) if recent else None,
        "errors": sum(1 for r in result.records if not r.ok),
        "dropped": result.dropped,
        "requests": len(result.records),
    }
    try:
        Path(path).write_text(json.dumps(payload))
    except OSError:
        pass


def histogram(records: list[RequestRecord]) -> list[dict]:
    """Log-spaced latency buckets: enough resolution for the tail without
    hauling every sample into the report."""
    bounds = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
    counts = [0] * (len(bounds) + 1)
    for r in records:
        for i, b in enumerate(bounds):
            if r.latency_ms <= b:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    out, lo = [], 0
    for i, b in enumerate(bounds):
        if counts[i]:
            out.append({"le_ms": b, "gt_ms": lo, "count": counts[i]})
        lo = b
    if counts[-1]:
        out.append({"le_ms": None, "gt_ms": bounds[-1], "count": counts[-1]})
    return out


def save_report(result: LoadResult, out: str | Path) -> None:
    report = {
        "target_rps": result.target_rps,
        "achieved_rps": round(len(result.records) / result.duration, 1) if result.duration else 0,
        "duration_s": round(result.duration, 1),
        "requests": len(result.records),
        "errors": sum(1 for r in result.records if not r.ok),
        "dropped": result.dropped,
        "p50_ms": round(result.percentile(50), 2),
        "p90_ms": round(result.percentile(90), 2),
        "p99_ms": round(result.percentile(99), 2),
        "p999_ms": round(result.percentile(99.9), 2),
        "histogram": histogram(result.records),
        "timeline": result.timeline(),
    }
    Path(out).write_text(json.dumps(report, indent=2))
