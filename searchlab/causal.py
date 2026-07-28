"""Connect the events that turn a GC pause into a dropped query.

The dashboard already charts JVM symptoms (heap, GC) and client outcomes
(latency, drops). What sits between them is the part a Solr admin actually
reasons through, and it was invisible:

    long GC pause -> ZooKeeper session lost -> replica marked down
        -> requests redirected / thread pool saturates -> queries fail

Each of those is observable, but individually they look like four
unrelated charts. This module records them on one timeline and links a
consequence back to the cause that plausibly produced it, so the answer
reads as a sequence rather than a coincidence.

Sources deliberately differ per event kind, because that is where the
truth actually lives: GC pauses and thread-pool saturation come from the
metrics snapshot, ZK session loss and replica transitions come from the
node logs (Solr does not expose them as metrics), and query failures come
from the load generator plus per-handler counters.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

# How long after a cause a consequence may appear and still be linked to it.
# A ZK session drops within a second or two of the pause that caused it; a
# replica goes down within a few seconds of that. Beyond this window the
# two are probably unrelated and saying otherwise would be a guess.
LINK_WINDOW_S = 12.0

# Ordered cause -> effect. Only adjacent kinds are linked, so the chain is
# built from steps that genuinely follow one another rather than from any
# two things that happened near each other.
CHAIN = ["gc_pause", "zk_lost", "replica_down", "threads_saturated", "queries_failing"]

SEVERITY = {
    "gc_pause": "warn",
    "zk_lost": "crit",
    "replica_down": "crit",
    "threads_saturated": "crit",
    "queries_failing": "crit",
    "recovery": "info",
    "leader_election": "info",
}

# Log lines that mark the middle of the chain. Solr reports these only in
# its logs, which is why the log stream is a first-class metric source here.
_LOG_EVENTS = [
    (re.compile(r"session expired|SessionExpired|zkClient has disconnected|"
                r"Our previous ZooKeeper session was expired", re.I),
     "zk_lost", "ZooKeeper session lost",
     "The node missed its heartbeats long enough for ZooKeeper to drop the "
     "session. ZooKeeper now considers it gone, which is what triggers "
     "replicas being marked down — usually a stop-the-world pause, not a "
     "network fault, on a single-machine lab."),
    (re.compile(r"Setting .*state to (down|recovery_failed)|"
                r"Marking .*as DOWN|is not live, but .*published as", re.I),
     "replica_down", "Replica marked down",
     "The cluster no longer routes to this replica. Its share of traffic "
     "shifts to the remaining ones, which is how one node's pause becomes "
     "everyone's latency."),
    (re.compile(r"Starting recovery|RecoveryStrategy|recoveringAfterStartup", re.I),
     "recovery", "Replica recovering",
     "The replica is catching up with its leader. Recovery competes for the "
     "same CPU and disk as live traffic, so it can prolong the incident it "
     "is resolving."),
    (re.compile(r"I am the new leader|LeaderElector|became leader", re.I),
     "leader_election", "Leader election",
     "A shard picked a new leader, which happens when the previous one "
     "stopped answering."),
]


def classify_log_event(line: str) -> tuple[str, str, str] | None:
    """(kind, title, detail) if this log line is a chain event."""
    for pattern, kind, title, detail in _LOG_EVENTS:
        if pattern.search(line):
            return kind, title, detail
    return None


class CausalTimeline:
    """Records chain events and links each one back to a plausible cause."""

    def __init__(self, maxlen: int = 400):
        self._events: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev_gc: dict[str, tuple[float, float]] = {}
        self._prev_handler: dict[str, tuple[float, int]] = {}
        self._open: dict[str, bool] = {}     # kind -> currently firing

    # ------------------------------------------------------------ record --

    def add(self, kind: str, title: str, detail: str, node: str | None = None,
            ts: float | None = None, **extra) -> dict:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "ts": ts, "kind": kind, "title": title,
                  "detail": detail, "node": node,
                  "severity": SEVERITY.get(kind, "info"), **extra}
            ev["caused_by"] = self._find_cause(kind, ts)
            self._events.append(ev)
            return ev

    def _find_cause(self, kind: str, ts: float) -> int | None:
        """The most recent preceding step of the chain, if it's recent enough."""
        if kind not in CHAIN:
            return None
        idx = CHAIN.index(kind)
        if idx == 0:
            return None
        earlier = set(CHAIN[:idx])
        for ev in reversed(self._events):
            if ts - ev["ts"] > LINK_WINDOW_S:
                break
            if ev["kind"] in earlier:
                return ev["seq"]
        return None

    def since(self, seq: int = 0) -> tuple[int, list[dict]]:
        with self._lock:
            return self._seq, [e for e in self._events if e["seq"] > seq]

    def recent(self, limit: int = 40) -> list[dict]:
        with self._lock:
            return list(self._events)[-limit:]

    # ------------------------------------------------- derive from metrics --

    def observe_snapshot(self, nodes: dict, loadtest: dict | None = None) -> None:
        """Derive chain events from a metrics snapshot.

        Called once per dashboard poll. Only *transitions* are recorded — a
        condition that stays true doesn't re-fire every two seconds, or the
        timeline would be unreadable.
        """
        latest = None
        server_errors = 0
        for name, n in (nodes or {}).items():
            if n.get("error"):
                continue
            ts = n.get("ts") or time.time()
            latest = ts if latest is None else max(latest, ts)
            self._check_gc(name, n, ts)
            self._check_threads(name, n, ts)
            # did Solr actually reject anything, or were queries just slow?
            for core in (n.get("cores") or {}).values():
                h = core.get("handler") or {}
                server_errors += (h.get("errors") or 0) + (h.get("timeouts") or 0)
        if loadtest:
            # stamp with the same clock the node events used, so a failure
            # links to the pause that caused it rather than falling outside
            # the window
            self._check_queries(loadtest, loadtest.get("ts") or latest,
                                server_errors)

    def _check_gc(self, node: str, n: dict, ts: float) -> None:
        """A GC pause long enough to threaten a ZK heartbeat."""
        try:
            total = sum(g.get("time", 0) for g in n["jvm"]["gc"].values())
        except (KeyError, TypeError, AttributeError):
            return
        prev = self._prev_gc.get(node)
        self._prev_gc[node] = (ts, total)
        if not prev:
            return
        prev_ts, prev_total = prev
        wall = ts - prev_ts
        paused = (total - prev_total) / 1000.0      # ms -> s
        if wall <= 0 or paused <= 0:
            return
        share = paused / wall
        # Worth reporting only when it's the kind of pause that breaks things:
        # a large absolute stall, or a big fraction of the interval.
        if paused >= 1.0 or share >= 0.30:
            self.add("gc_pause",
                     f"{node} paused {paused:.1f}s for garbage collection",
                     f"That is {share:.0%} of the last {wall:.0f}s. ZooKeeper's "
                     f"session timeout is measured in seconds, so pauses this "
                     f"long risk the node being declared dead while it is "
                     f"merely frozen.",
                     node=node, ts=ts, seconds=round(paused, 2))

    def _check_threads(self, node: str, n: dict, ts: float) -> None:
        """Request threads all busy, with requests queueing behind them."""
        t = n.get("threads") or {}
        util = t.get("utilization")
        queued = t.get("queued") or 0
        if util is None:
            return
        saturated = util >= 0.95 and queued > 0
        key = f"threads:{node}"
        was = self._open.get(key, False)
        self._open[key] = saturated
        if saturated and not was:
            self.add("threads_saturated",
                     f"{node} request threads saturated",
                     f"Every request thread is busy and {queued} request(s) are "
                     f"queued behind them. Requests that cannot get a thread "
                     f"are rejected — this produces dropped queries with no GC "
                     f"pause to blame.",
                     node=node, ts=ts, queued=queued)

    def _check_queries(self, lt: dict, ts: float | None = None,
                       server_errors: int = 0) -> None:
        """The client-side effect: requests failing or never sent.

        Distinguishing *who* failed matters. If Solr's own handler counters
        show no errors, nothing was rejected — the queries were merely too
        slow, and the client's in-flight cap filled up. That is a capacity
        problem, not a rejection, and it points at different fixes.
        """
        dropped = lt.get("dropped") or 0
        errors = lt.get("errors") or 0
        failing = dropped + errors
        was = self._open.get("queries", False)
        now = failing > 0
        self._open["queries"] = now
        if not (now and not was):
            return
        if dropped and not server_errors:
            detail = ("Solr rejected none of these — its own error and timeout "
                      "counters are zero. The load generator refused to send "
                      "requests it had scheduled because too many were still "
                      "awaiting a reply, which means responses were too slow, "
                      "not that the cluster turned work away.")
        else:
            detail = ("Solr itself reported errors or timeouts, so requests "
                      "were actively rejected rather than merely delayed — "
                      "look at thread pool saturation and circuit breakers "
                      "above.")
        self.add("queries_failing",
                 f"{failing} request(s) failed or were dropped",
                 detail, ts=ts, errors=errors, dropped=dropped)

    # ------------------------------------------------------------ stories --

    def chains(self, limit: int = 6) -> list[list[dict]]:
        """Group linked events into sequences, newest first.

        A chain is a run of events connected by caused_by, which is what
        turns four separate charts into one explanation.
        """
        with self._lock:
            events = list(self._events)
        by_seq = {e["seq"]: e for e in events}
        children: dict[int, list[dict]] = {}
        roots = []
        for e in events:
            parent = e.get("caused_by")
            if parent and parent in by_seq:
                children.setdefault(parent, []).append(e)
            else:
                roots.append(e)

        def walk(ev):
            out = [ev]
            for child in children.get(ev["seq"], []):
                out += walk(child)
            return out

        chains = [walk(r) for r in roots]
        chains = [c for c in chains if len(c) > 1]     # a lone event is no story
        chains.sort(key=lambda c: -c[0]["ts"])
        return chains[:limit]
