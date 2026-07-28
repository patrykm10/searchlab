"""Live cluster log tailing for the dashboard.

Streams `docker compose logs --follow` in a daemon thread into a bounded
ring buffer; the dashboard serves increments from it via /api/logs?since=N.
Container stdout is the one log source that exists regardless of the
`--gc-logs` flag, and it carries the events people watch a lab for:
commits, merges, leader elections, replica recovery, warnings.
"""

from __future__ import annotations

import random
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .loglex import classify, is_noise, parse_request

# A genuine Solr log record always opens with a timestamp + level, e.g.
# "searchlab-solr1  | 2026-07-21 06:41:05.488 INFO  (...)". Some operations
# (ADDREPLICA, cluster state writes) log a pretty-printed multi-line JSON
# blob instead of one line; every line after the first lacks this header
# and would otherwise show up as its own contextless fragment. Lines
# without the header are continuations of whichever record came before.
_RECORD_START = re.compile(r"\|\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\w+)")
# compose prefixes each line with the container it came from
_RECORD_NODE = re.compile(r"^\s*\S*?(solr\d+|zk\d+)\s*\|")


class LogStream:
    """Tail cluster logs into two ring buffers.

    *Activity* holds cluster lifecycle events as (seq, line, event) triples,
    where event is loglex's plain-language tag or None. *Traffic* holds the
    queries and index requests parsed out of the request log as (seq, entry)
    pairs — noise in the activity feed, but the whole point of the traffic
    panel, so they're routed rather than dropped.
    """

    # optional CausalTimeline; log lines are the only place Solr reports ZK
    # session loss and replica state changes, so the chain needs this feed
    causal = None

    def __init__(self, compose_file: Path, maxlen: int = 2000,
                 traffic_maxlen: int = 500):
        self.compose_file = compose_file
        self._buf: deque[tuple[int, str, dict | None]] = deque(maxlen=maxlen)
        self._seq = 0
        self._traffic: deque[tuple[int, dict]] = deque(maxlen=traffic_maxlen)
        self._traffic_seq = 0
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stopping = False
        self._drop_continuation = False
        self.error: str | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def since(self, seq: int) -> tuple[int, list[list]]:
        """Entries newer than `seq`, plus the latest seq for the next cursor."""
        with self._lock:
            lines = [[s, line, event] for s, line, event in self._buf if s > seq]
            return self._seq, lines

    def traffic_since(self, seq: int) -> tuple[int, list[list]]:
        """Traffic entries newer than `seq`, plus the latest traffic seq."""
        with self._lock:
            rows = [[s, entry] for s, entry in self._traffic if s > seq]
            return self._traffic_seq, rows

    def _append(self, line: str) -> None:
        text = line.rstrip("\n")
        m = _RECORD_START.search(text)
        if m is None:
            # Continuation of the previous record (e.g. a multi-line JSON
            # blob or a stack trace). Skip fragments of routine INFO records
            # we already dropped or chose not to expand; keep continuations
            # of WARN/ERROR records, where the follow-on lines are the detail.
            if self._drop_continuation:
                return
        else:
            self._drop_continuation = is_noise(text) or m.group(1) == "INFO"

        request = parse_request(text)
        if request is not None:
            # Per-shard fan-out would show every query two or three times.
            if not request["internal"]:
                with self._lock:
                    self._traffic_seq += 1
                    self._traffic.append((self._traffic_seq, request))
            return
        if is_noise(text):
            return
        if self.causal is not None:
            from .causal import classify_log_event

            found = classify_log_event(text)
            if found:
                kind, title, detail = found
                node = _RECORD_NODE.search(text)
                self.causal.add(kind, title, detail,
                                node=node.group(1) if node else None)
        with self._lock:
            self._seq += 1
            self._buf.append((self._seq, text, classify(text)))

    def _run(self) -> None:
        from .cluster import _compose_cmd

        try:
            compose = _compose_cmd()
        except SystemExit as e:
            self.error = str(e)
            return
        while not self._stopping:
            try:
                self._proc = subprocess.Popen(
                    compose + ["-f", str(self.compose_file), "logs",
                               "--follow", "--tail=50", "--no-color"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace",
                )
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    self._append(line)
            except OSError as e:
                self.error = str(e)
            if self._stopping:
                return
            self._append("[log stream ended — is the cluster running?]")
            time.sleep(5)


class FakeLogStream(LogStream):
    """Synthesized log lines so `--demo` previews the panel without docker."""

    _TEMPLATES = [
        "searchlab-solr1  | {ts} INFO  (qtp-45) [c:products] o.a.s.u.DirectUpdateHandler2 start commit{{flags=0,optimize=false,openSearcher=true,waitSearcher=true}}",
        "searchlab-solr1  | {ts} INFO  (qtp-45) [c:products] o.a.s.u.DirectUpdateHandler2 end_commit_flush",
        "searchlab-solr2  | {ts} INFO  (searcherExecutor-22) [c:products] o.a.s.c.SolrCore [products_shard2_replica_n2] Registered new searcher",
        "searchlab-solr1  | {ts} INFO  (Lucene Merge Thread #3) o.a.s.u.LoggingInfoStream [MS] merge segment _4c into _4d",
        "searchlab-solr2  | {ts} INFO  (coreZkRegister-1) o.a.s.c.RecoveryStrategy Starting recovery process. recoveringAfterStartup=true",
        "searchlab-solr1  | {ts} WARN  (qtp-12) o.a.s.h.a.AdminHandlersProxy Timeout occurred while waiting for a response",
        "searchlab-zk1    | {ts} INFO  (QuorumPeer) Notification: my state:LOOKING; n.leader: 1",
        "searchlab-solr2  | {ts} INFO  (qtp-27) [c:products] o.a.s.u.p.LogUpdateProcessorFactory [products_shard1_replica_n1] webapp=/solr path=/update params={{}} status=0 QTime=14",
    ]

    def __init__(self, maxlen: int = 2000):
        super().__init__(compose_file=Path("/dev/null"), maxlen=maxlen)
        self._rng = random.Random(7)

    def _run(self) -> None:
        while not self._stopping:
            ts = time.strftime("%Y-%m-%d %H:%M:%S") + ".000"
            self._append(self._rng.choice(self._TEMPLATES).format(ts=ts))
            time.sleep(self._rng.uniform(0.5, 2.0))
