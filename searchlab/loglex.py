"""Recognize common Solr log events and describe them in plain language.

The activity log is where the lab's cause-and-effect becomes visible —
click Commit, see the commit — but raw Solr INFO lines are unreadable to
anyone who doesn't already know Solr. classify() tags the recognizable
events so the UI can label them and explain, on hover, what actually
happened. Unrecognized lines pass through untagged.

Order matters: first match wins, so put the more specific patterns first.
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

_EVENTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"start commit.*openSearcher=true", re.I),
     "commit",
     "Saving recent changes and opening a new searcher — documents added "
     "since the last commit become searchable when this finishes."),
    (re.compile(r"start commit", re.I),
     "commit",
     "Saving recent changes to disk (hard commit). Durability, not "
     "visibility — a separate searcher open makes them searchable."),
    (re.compile(r"end_commit_flush", re.I),
     "commit done",
     "The commit finished writing — the index files on disk now include "
     "the recent changes."),
    (re.compile(r"Registered new searcher", re.I),
     "new searcher",
     "The node switched to a fresh view of the index — queries from now "
     "on see the latest committed documents. Caches restart from cold "
     "(or pre-warmed) here."),
    (re.compile(r"newest segment|merged segment|merge(d|s)? .*segment|\[MS\]|TieredMergePolicy", re.I),
     "merge",
     "Background segment merge — the engine is compacting index files "
     "into fewer, larger ones. Normal housekeeping; heavy merges can "
     "briefly slow indexing and queries."),
    (re.compile(r"I am the new leader|became leader|leader election|LeaderElector", re.I),
     "leader election",
     "The shard is (re)electing which replica coordinates writes. Happens "
     "at startup and whenever the current leader disappears."),
    (re.compile(r"Starting recovery|RecoveryStrategy|Finished recovery|recoveringAfterStartup", re.I),
     "recovery",
     "A replica is catching up with its shard leader after being down or "
     "falling behind — copying missed updates until it's in sync."),
    (re.compile(r"CoreContainer.*Creating.*core|Opening new SolrCore", re.I),
     "core created",
     "A new index core is starting on this node — typically a new "
     "collection, shard, or replica being set up."),
    (re.compile(r"(Unloading|Deleting|removed) core|CLOSING SolrCore", re.I),
     "core removed",
     "An index core is being shut down and removed from this node — "
     "usually a replica or collection being deleted."),
    (re.compile(r"path=/update.*commit=true", re.I),
     "commit",
     "An explicit commit request — asking the engine to save recent "
     "changes and make them searchable now."),
    (re.compile(r"path=/update", re.I),
     "indexing",
     "A batch of documents arriving to be indexed."),
    (re.compile(r"zkClient has (dis)?connected|Watcher .* fired|SyncConnected", re.I),
     "zookeeper",
     "Cluster-coordination traffic with ZooKeeper — nodes agreeing on "
     "who is alive and who leads each shard."),
]


def classify(line: str) -> dict | None:
    """Tag a log line: {"tag": short label, "desc": what it means} or None."""
    for pattern, tag, desc in _EVENTS:
        if pattern.search(line):
            return {"tag": tag, "desc": desc}
    return None


# Solr's request logger records every incoming call. The only one worth
# showing in the activity panel is indexing (/update); the rest — /select
# queries, and the /config, /config/overlay, /admin/* polling that the
# dashboard itself generates — is chatter the charts already cover. So we
# drop INFO request-log lines whose path isn't /update. WARN/ERROR lines
# are never dropped (they don't carry " INFO "), whatever their path.
_REQUEST_PATH = re.compile(r" INFO .*\bpath=(\S+)", re.I)


def is_noise(line: str) -> bool:
    """True for routine INFO request-log lines the activity panel should skip."""
    m = _REQUEST_PATH.search(line)
    if not m:
        return False
    path = m.group(1).rstrip(",")
    return not path.startswith("/update")


# --------------------------------------------------------------- traffic ---
#
# Request-log lines are noise in the *activity* feed but they're exactly what
# the traffic panel exists to show: the actual queries and index requests
# hitting the cluster. parse_request() turns them into structured entries.
#
# One wrinkle worth knowing: a distributed query fans out into per-shard
# sub-requests, each logged separately and carrying isShard=true. Showing
# those would triple every query in the panel, so they're flagged internal
# and dropped — the panel shows the query the client actually sent.

_TIME = re.compile(r"(\d{2}:\d{2}:\d{2})")
_NODE_PREFIX = re.compile(r"^\s*\S*?(solr\d+|zk\d+)\s*\|")
_CORE = re.compile(r"\bx:([^\s\]]+)")
_TAIL_INTS = re.compile(r"\s(\d+)\s+(\d+)\s*$")

# params not worth showing: transport/plumbing rather than intent
_DULL_PARAMS = {
    "wt", "rid", "version", "df", "distrib", "shards.purpose", "NOW",
    "omitHeader", "isShard", "fsv", "_stateVer_", "overwrite", "commitWithin",
    "update.distrib", "distrib.from", "waitSearcher", "openSearcher",
}


def _balanced(text: str, start: int) -> tuple[str, str] | None:
    """Content of the {...} beginning at `start`, plus the remainder after it."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], text[i + 1:]
    return None


def _split_params(raw: str) -> list[tuple[str, str]]:
    out = []
    for pair in raw.split("&"):
        if not pair:
            continue
        key, _, val = pair.partition("=")
        out.append((key, unquote_plus(val)))
    return out


def parse_request(line: str) -> dict | None:
    """Structured traffic entry for a /select or /update log line, else None.

    Entries carry `internal: True` when they're per-shard fan-out rather than
    a client-issued request, so callers can drop them.
    """
    idx = line.find("params={")
    if idx == -1:
        return None
    path_m = re.search(r"\bpath=(/\S+)", line)
    if not path_m:
        return None
    path = path_m.group(1)
    kind = "query" if path.startswith("/select") else \
           "index" if path.startswith("/update") else None
    if kind is None:
        return None

    got = _balanced(line, idx + len("params="))
    if got is None:
        return None
    raw_params, rest = got
    params = _split_params(raw_params)
    flat = dict(params)

    entry: dict = {
        "kind": kind,
        "time": (_TIME.search(line).group(1) if _TIME.search(line) else ""),
        "node": (_NODE_PREFIX.match(line).group(1) if _NODE_PREFIX.match(line) else ""),
        "core": (_CORE.search(line).group(1) if _CORE.search(line) else ""),
        # isShard: a query's per-shard fan-out. update.distrib: the leader
        # replicating a batch onward. Both are internal hops of a request the
        # client made once, so showing them would double-count the traffic.
        "internal": flat.get("isShard") == "true" or "update.distrib" in flat,
    }

    if kind == "query":
        shown = [f"{k}={v}" for k, v in params if k not in _DULL_PARAMS and v]
        entry["detail"] = "  ".join(shown) or "*:*"
        hits = re.search(r"\bhits=(\d+)", rest)
        status = re.search(r"\bstatus=(\d+)", rest)
        qtime = re.search(r"\bQTime=(\d+)", rest)
        entry["hits"] = int(hits.group(1)) if hits else None
        entry["status"] = int(status.group(1)) if status else None
        entry["qtime"] = int(qtime.group(1)) if qtime else None
        return entry

    # /update: a {add=[...]} / {delete=[...]} block, then "<status> <qtime>"
    cmd_start = rest.find("{")
    docs, verb = None, "update"
    if cmd_start != -1:
        got_cmd = _balanced(rest, cmd_start)
        if got_cmd:
            cmd, rest = got_cmd[0], got_cmd[1]
            verb_m = re.match(r"\s*(\w+)=", cmd)
            if verb_m:
                verb = verb_m.group(1)
            # Solr logs only the first few doc ids then states the real
            # total: "{add=[doc-0, doc-1, ... (500 adds)]}". Trust that
            # total — counting the listed ids undercounts every batch.
            total = re.search(r"\((\d+)\s+adds?\)", cmd)
            if total:
                docs = int(total.group(1))
            else:
                listed = re.search(r"=\[(.*)\]", cmd, re.S)
                if listed and listed.group(1).strip():
                    docs = listed.group(1).count(",") + 1
    entry["detail"] = (f"{docs:,} docs {verb.replace('add', 'added')}" if docs
                       else verb)
    entry["docs"] = docs
    tail = _TAIL_INTS.search(rest)
    entry["status"] = int(tail.group(1)) if tail else None
    entry["qtime"] = int(tail.group(2)) if tail else None
    entry["hits"] = None
    return entry
