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
