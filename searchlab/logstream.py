"""Live cluster log tailing for the dashboard.

Streams `docker compose logs --follow` in a daemon thread into a bounded
ring buffer; the dashboard serves increments from it via /api/logs?since=N.
Container stdout is the one log source that exists regardless of the
`--gc-logs` flag, and it carries the events people watch a lab for:
commits, merges, leader elections, replica recovery, warnings.
"""

from __future__ import annotations

import random
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .loglex import classify


class LogStream:
    """Tail cluster logs into a ring buffer of (seq, line, event) triples,
    where event is loglex's plain-language tag or None."""

    def __init__(self, compose_file: Path, maxlen: int = 2000):
        self.compose_file = compose_file
        self._buf: deque[tuple[int, str, dict | None]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stopping = False
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

    def _append(self, line: str) -> None:
        text = line.rstrip("\n")
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
        "solr1  | INFO  o.a.s.u.DirectUpdateHandler2 start commit{flags=0,optimize=false,openSearcher=true,waitSearcher=true}",
        "solr1  | INFO  o.a.s.u.DirectUpdateHandler2 end_commit_flush",
        "solr2  | INFO  o.a.s.c.SolrCore [products_shard2_replica_n2] Registered new searcher",
        "solr1  | INFO  o.a.s.u.LoggingInfoStream [MS][qtp123-45]: merge segment _4c into _4d",
        "solr2  | INFO  o.a.s.s.HttpSolrCall [admin] webapp=null path=/admin/metrics status=0 QTime=3",
        "solr1  | WARN  o.a.s.h.a.AdminHandlersProxy Timeout occurred while waiting for a response",
        "zk1    | INFO  Notification: my state:LOOKING; n.leader: 1",
        "solr2  | INFO  o.a.s.c.S.Request [products_shard1_replica_n1] webapp=/solr path=/select params={q=body_t:merge} hits=42 status=0 QTime=11",
    ]

    def __init__(self, maxlen: int = 2000):
        super().__init__(compose_file=Path("/dev/null"), maxlen=maxlen)
        self._rng = random.Random(7)

    def _run(self) -> None:
        while not self._stopping:
            self._append(self._rng.choice(self._TEMPLATES))
            time.sleep(self._rng.uniform(0.5, 2.0))
