"""Parse JVM unified GC logs (JDK 9+) and tell the pause story.

Pairs with `searchlab up --gc-logs`, which mounts each node's log directory to
`.searchlab/gc-logs/<node>/`. The numbers that matter for a Solr incident:
pause count and total, the pause tail (max, p99), throughput lost to GC, and
whether Full GCs happened at all (in a healthy cluster they shouldn't).

Handles lines like:
[2026-07-09T12:00:01.123+0000][12.345s][info][gc] GC(5) Pause Young (Normal) \
    (G1 Evacuation Pause) 512M->128M(1024M) 12.345ms
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_PAUSE = re.compile(
    r"\[(?P<uptime>[\d.]+)s\].*?GC\((?P<num>\d+)\)\s+Pause\s+(?P<kind>Young|Full|Remark|Cleanup|Initial Mark)"
    r"(?P<detail>.*?)(?:(?P<before>\d+)(?P<u1>[KMG])->(?P<after>\d+)(?P<u2>[KMG])\((?P<total>\d+)(?P<u3>[KMG])\))?\s+"
    r"(?P<ms>[\d.]+)ms"
)

_MB = {"K": 1 / 1024, "M": 1.0, "G": 1024.0}


@dataclass
class Pause:
    uptime_s: float
    kind: str
    ms: float
    reclaimed_mb: float | None


def parse_gclog(path: str | Path) -> list[Pause]:
    pauses = []
    with open(path, errors="replace") as f:
        for line in f:
            m = _PAUSE.search(line)
            if not m:
                continue
            reclaimed = None
            if m.group("before"):
                reclaimed = (int(m.group("before")) * _MB[m.group("u1")]
                             - int(m.group("after")) * _MB[m.group("u2")])
            pauses.append(Pause(
                uptime_s=float(m.group("uptime")),
                kind=m.group("kind"),
                ms=float(m.group("ms")),
                reclaimed_mb=reclaimed,
            ))
    return pauses


def _pct(sorted_ms: list[float], p: float) -> float:
    if not sorted_ms:
        return float("nan")
    return sorted_ms[min(int(len(sorted_ms) * p / 100), len(sorted_ms) - 1)]


def summarize(pauses: list[Pause], label: str = "") -> str:
    if not pauses:
        return f"{label}: no GC pauses found"
    wall = pauses[-1].uptime_s - pauses[0].uptime_s or 1
    total_ms = sum(p.ms for p in pauses)
    lines = [f"{label}  ({len(pauses)} pauses over {wall:.0f}s of JVM uptime)"]
    lines.append(
        f"  total pause {total_ms:.0f} ms — throughput lost to GC: "
        f"{total_ms / (wall * 1000) * 100:.2f}%"
    )
    by_kind: dict[str, list[float]] = {}
    for p in pauses:
        by_kind.setdefault(p.kind, []).append(p.ms)
    for kind, ms in sorted(by_kind.items()):
        s = sorted(ms)
        lines.append(
            f"  {kind:<12} n={len(s):<5} p50={_pct(s, 50):.1f}ms  "
            f"p99={_pct(s, 99):.1f}ms  max={s[-1]:.1f}ms"
        )
    fulls = by_kind.get("Full", [])
    if fulls:
        lines.append(
            f"  !! {len(fulls)} Full GC(s) — heap too small for the workload, "
            f"or something is leaking searchers/caches"
        )
    reclaims = [p.reclaimed_mb for p in pauses if p.reclaimed_mb is not None]
    if reclaims:
        lines.append(f"  avg reclaimed per collection: {sum(reclaims) / len(reclaims):.0f} MB")
    worst = max(pauses, key=lambda p: p.ms)
    lines.append(f"  worst pause: {worst.ms:.1f} ms ({worst.kind}) at uptime {worst.uptime_s:.0f}s")
    return "\n".join(lines)


def find_gclogs(root: str | Path) -> dict[str, list[Path]]:
    """Map node name -> gc log files under .searchlab/gc-logs/<node>/."""
    root = Path(root)
    out: dict[str, list[Path]] = {}
    if not root.exists():
        sys.exit(f"searchlab: {root} not found — start the cluster with `searchlab up --gc-logs`")
    for node_dir in sorted(root.iterdir()):
        if node_dir.is_dir():
            # Solr writes solr_gc.log*; ES/OS write gc.log* in their logs dir
            logs = sorted(node_dir.glob("solr_gc.log*")) or sorted(node_dir.glob("gc.log*"))
            if logs:
                out[node_dir.name] = logs
    if not out:
        sys.exit(f"searchlab: no GC logs (solr_gc.log*/gc.log*) under {root} yet")
    return out
