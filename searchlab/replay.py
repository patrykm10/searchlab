"""Replay real query traffic against a lab cluster.

The gap between synthetic templates and a production incident is the actual
query mix: the weird fq combinations, the deep pages, the facet storms. This
module parses Solr request logs (the standard `o.a.s.c.S.Request` lines) or
plain files (one query string per line) and replays them open-loop — at the
original pacing from the log timestamps, time-scaled (`speed=2.0` replays a
10-minute log in 5), or flattened to a fixed RPS.

Reuses LoadResult, so summaries, JSON reports, HTML reports, and `compare`
all work on replays. Statistical templates answer "how does it behave under
load"; a replay answers "what happened last Tuesday".
"""

from __future__ import annotations

import asyncio
import json as _json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote

import httpx

from .loadtest import LoadResult, RequestRecord

# 2026-07-09 12:00:01.123 INFO (qtp..) [c:products s:shard1 ...] o.a.s.c.S.Request
#   webapp=/solr path=/select params={q=foo&fq=bar} hits=12 status=0 QTime=3
_SOLR_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d{3}).*?"
    r"o\.a\.s\.c\.S\.Request.*?path=(?P<path>/\S+)\s+params=\{(?P<params>.*?)\}"
)

# Classic ES/OS search slow log:
# [2026-07-09T12:00:00,123][WARN ][i.s.s.query] [node1] [products][0] took[12ms],
#   took_millis[12], ... source[{"query":{"match_all":{}}}]
_SLOWLOG_LINE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.,]\d{3})[^\]]*\]"
    r".*?took(?:_millis)?\[.*?source\[(?P<src>\{.*\})\]", re.IGNORECASE
)


def _parse_es_line(line: str) -> dict | None:
    """One slow-log entry from either classic or JSON-lines format."""
    m = _SLOWLOG_LINE.search(line)
    if m:
        return {"ts": _parse_ts(m.group("ts").replace("T", " ")),
                "body": _json.loads(m.group("src"))}
    if line.startswith("{"):
        try:
            obj = _json.loads(line)
        except ValueError:
            return None
        src = obj.get("elasticsearch.slowlog.source") or obj.get("source")
        ts = obj.get("@timestamp") or obj.get("timestamp")
        if src and ts:
            body = _json.loads(src) if isinstance(src, str) else src
            return {"ts": _parse_ts(ts.replace("Z", "").replace("T", " ")[:23]),
                    "body": body}
    return None


def _parse_ts(raw: str) -> float:
    raw = raw.replace(",", ".").replace("T", " ")
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f").replace(
        tzinfo=timezone.utc
    ).timestamp()


def _parse_params(raw: str) -> list[tuple[str, str]]:
    # Solr logs URL-encode param values inside the braces; decode and keep
    # repeated keys (multiple fq's matter).
    return [(k, unquote(v)) for k, v in parse_qsl(raw, keep_blank_values=True)]


def parse_log(path: str | Path, path_filter: str = "/select",
              engine: str = "solr") -> list[dict]:
    """Return timed entries sorted by offset. Formats by engine:
    solr — request logs (`o.a.s.c.S.Request` lines) or plain query strings;
    es/os — search slow logs (classic bracketed or JSON-lines)."""
    entries: list[dict] = []
    fmt = None  # detected per file: "solr" | "plain" | "es"
    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if engine != "solr":
                e = _parse_es_line(line)
                if e:
                    entries.append(e)
                continue
            m = _SOLR_LINE.search(line)
            if fmt is None:
                fmt = "solr" if m else "plain"
            if fmt == "solr":
                if not m:
                    continue  # non-request lines interleaved in the log
                if path_filter and m.group("path") != path_filter:
                    continue
                entries.append({
                    "ts": _parse_ts(m.group("ts")),
                    "path": m.group("path"),
                    "params": _parse_params(m.group("params")),
                })
            else:
                entries.append({"ts": float(i), "path": path_filter,
                                "params": _parse_params(line)})
    if not entries:
        sys.exit(f"searchlab: no replayable queries found in {path}")
    entries.sort(key=lambda e: e["ts"])
    t0 = entries[0]["ts"]
    for e in entries:
        e["offset_s"] = e.pop("ts") - t0
    return entries


async def replay(
    base_url: str,
    collection: str,
    entries: list[dict],
    speed: float = 1.0,
    rps: float | None = None,
    loop_count: int = 1,
    max_in_flight: int = 500,
    timeout: float = 30.0,
) -> LoadResult:
    """Open-loop replay. `rps` overrides log pacing with a uniform schedule;
    `speed` scales the original pacing. `loop_count` repeats the log."""
    schedule: list[tuple[float, dict]] = []
    if rps:
        for loop_i in range(loop_count):
            base = loop_i * len(entries) / rps
            for j, e in enumerate(entries):
                schedule.append((base + j / rps, e))
        total = loop_count * len(entries) / rps
    else:
        span = (entries[-1]["offset_s"] / speed) if len(entries) > 1 else 0
        gap = span / max(len(entries), 1) or 0.001  # loop spacing for 1-query logs
        for loop_i in range(loop_count):
            base = loop_i * (span + gap)
            for e in entries:
                schedule.append((base + e["offset_s"] / speed, e))
        total = loop_count * (span + gap)

    result = LoadResult(target_rps=len(schedule) / total if total else len(schedule))
    in_flight = 0
    tasks: set[asyncio.Task] = set()

    async def fire(client: httpx.AsyncClient, scheduled: float, entry: dict) -> None:
        nonlocal in_flight
        start = time.perf_counter()
        try:
            if "body" in entry:
                r = await client.post(f"{base_url}/{collection}/_search", json=entry["body"])
                label = "/_search"
            else:
                r = await client.get(
                    f"{base_url}/{collection}{entry['path']}",
                    params=entry["params"] + [("wt", "json")],
                )
                label = entry["path"]
            ok, status = r.status_code == 200, r.status_code
        except httpx.HTTPError:
            ok, status = False, 0
        result.records.append(RequestRecord(
            scheduled, (time.perf_counter() - start) * 1000, status, label, ok))
        in_flight -= 1

    async with httpx.AsyncClient(
        timeout=timeout, limits=httpx.Limits(max_connections=max_in_flight)
    ) as client:
        t0 = time.perf_counter()
        for at, entry in schedule:
            wait = at - (time.perf_counter() - t0)
            if wait > 0:
                await asyncio.sleep(wait)
            if in_flight >= max_in_flight:
                result.dropped += 1
                continue
            in_flight += 1
            t = asyncio.create_task(fire(client, time.perf_counter() - t0, entry))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        result.duration = time.perf_counter() - t0
    return result
