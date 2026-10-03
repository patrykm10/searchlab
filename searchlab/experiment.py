"""Change one thing, keep everything else the same, and see what moved.

An A/B/A run on the live cluster. Measure with the current value of one knob
(A), change it (B), measure, change it back and measure again (A'). All three
phases replay the same seeded query sequence at the same rate.

Two things make the comparison fair, and both are easy to get wrong by hand:

- Turning a knob reloads the core, and a reload empties the caches. So every
  phase starts from a reload, including A, and a warm-up is run and thrown
  away before measuring. Otherwise A runs on warm caches and B on cold ones,
  and the "result" is the cache flush.
- A and A' have the same setting, so how far apart they land is the noise:
  what the numbers do when nothing changed. A difference between B and A
  smaller than that is not a result, however large it looks as a percentage.

Solr only for now: the reload, the knob reads and the cache/GC counters are
Solr's. On ES/OS the command says so rather than measuring something else.
"""

from __future__ import annotations

import asyncio
import statistics
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from . import metrics as m
from . import tuning
from .cluster import ClusterSpec
from .loadtest import LoadResult, run_load

CACHES = ("filterCache", "queryResultCache", "documentCache")


def _reload(spec: ClusterSpec, collection: str) -> None:
    r = httpx.get(f"{spec.base_url()}/admin/collections",
                  params={"action": "RELOAD", "name": collection, "wt": "json"}, timeout=120)
    r.raise_for_status()


def _set(spec: ClusterSpec, collection: str, knob: str, value: float | None) -> None:
    """Apply a value, or return the knob to what solrconfig.xml says (None)."""
    if value is not None:
        tuning.apply_tuning(spec, collection, knob, value)
        return
    k = tuning.KNOBS[knob]
    command = "unset-user-property" if k.get("user_prop") else "unset-property"
    r = httpx.post(f"{spec.base_url()}/{collection}/config",
                   json={command: k["path"]}, timeout=30)
    r.raise_for_status()


def _overridden(spec: ClusterSpec, collection: str, knob: str) -> bool:
    """Is this knob set in the configset's overlay, or read from solrconfig.xml?
    Putting a knob back means returning it to whichever of those it was:
    writing the file's value into the overlay would leave an override behind
    where there was none."""
    r = httpx.get(f"{spec.base_url()}/{collection}/config/overlay",
                  params={"wt": "json"}, timeout=30)
    r.raise_for_status()
    overlay = r.json().get("overlay", {})
    k = tuning.KNOBS[knob]
    if k.get("user_prop"):
        return k["path"] in overlay.get("userProps", {})
    node = overlay.get("props", {})
    for part in k["path"].split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _sharing(spec: ClusterSpec, collection: str) -> tuple[str, list[str]]:
    r = httpx.get(f"{spec.base_url()}/admin/collections",
                  params={"action": "CLUSTERSTATUS", "wt": "json"}, timeout=30)
    r.raise_for_status()
    colls = r.json()["cluster"]["collections"]
    name = (colls.get(collection) or {}).get("configName", "")
    return name, sorted(c for c, v in colls.items()
                        if c != collection and v.get("configName") == name)


def _wait_for(spec: ClusterSpec, collection: str, knob: str, value: float | None,
              timeout: float = 60.0) -> None:
    # the Config API returns before every core has reloaded; measuring then
    # would time part of the phase on the old value
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if tuning.read_tuning(spec, collection).get(knob) == value:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    raise RuntimeError(f"{knob} did not read back as {value} within {timeout:.0f}s")


def _counters(spec: ClusterSpec, collection: str) -> dict:
    """GC totals across the cluster, and cache hit ratios for this
    collection's cores (cumulative since the core's last reload)."""
    nodes = m.snapshot_cluster(spec)
    gc_ms = gc_n = 0
    ratios: dict[str, list[float]] = {c: [] for c in CACHES}
    for node in nodes.values():
        if "error" in node:
            continue
        for g in (node.get("jvm") or {}).get("gc", {}).values():
            gc_ms += g.get("time") or 0
            gc_n += g.get("count") or 0
        for core, stats in (node.get("cores") or {}).items():
            # metrics name cores <collection>.<shard>.<replica>
            if not core.startswith(collection + "."):
                continue
            for cache in CACHES:
                hr = (stats.get("caches") or {}).get(cache, {}).get("hitratio")
                if isinstance(hr, (int, float)):
                    ratios[cache].append(hr)
    return {"gc_ms": gc_ms, "gc_pauses": gc_n,
            "hitratio": {c: (sum(v) / len(v) if v else None) for c, v in ratios.items()}}


def worst_second(records: list, warmup: float) -> dict:
    """The one-second window holding the most of a phase's slowest requests,
    and p99 recomputed without it. A single stall (a GC pause, a merge,
    another collection reloading) can own p99 on its own; if p99 falls back
    once that second is left out, the phase had a burst, not a shift."""
    if len(records) < 100:
        return {}
    lats = sorted(r.latency_ms for r in records)
    slow = lats[min(int(len(lats) * 0.99), len(lats) - 1)]
    by_s: dict[int, int] = {}
    for r in records:
        if r.latency_ms >= slow:
            sec = int(r.scheduled - warmup)
            by_s[sec] = by_s.get(sec, 0) + 1
    sec, n = max(by_s.items(), key=lambda kv: (kv[1], -kv[0]))
    rest = sorted(r.latency_ms for r in records if int(r.scheduled - warmup) != sec)
    return {"worst_s": sec, "worst_slow": n,
            "p99_without_worst_s": rest[min(int(len(rest) * 0.99), len(rest) - 1)]}


def measure(spec: ClusterSpec, collection: str, rps: float, warmup: float, duration: float,
            seed: int, queries_path: str | Path | None) -> dict:
    before = _counters(spec, collection)
    result: LoadResult = asyncio.run(run_load(
        spec.base_url(), collection, rps, duration=warmup + duration,
        queries_path=queries_path, seed=seed, engine=spec.engine))
    after = _counters(spec, collection)
    kept = [r for r in result.records if r.scheduled >= warmup]
    ok = [r for r in kept if r.ok]
    return {
        "requests": len(kept),
        "errors": len(kept) - len(ok),
        "dropped": result.dropped,
        "achieved_rps": len(kept) / duration if duration else 0.0,
        "p50_ms": result.percentile(50, ok) if ok else None,
        "p90_ms": result.percentile(90, ok) if ok else None,
        "p99_ms": result.percentile(99, ok) if ok else None,
        **worst_second(ok, warmup),
        "gc_ms": after["gc_ms"] - before["gc_ms"],
        "gc_pauses": after["gc_pauses"] - before["gc_pauses"],
        "hitratio": after["hitratio"],
    }


def run_experiment(spec: ClusterSpec, collection: str, knob: str, to: float, *,
                   rps: float = 50.0, duration: float = 30.0, warmup: float = 10.0,
                   seed: int = 7, queries_path: str | Path | None = None, settle: float = 3.0,
                   rounds: int = 1, say: Callable[[str], None] = print) -> dict:
    if spec.engine != "solr":
        raise SystemExit("searchlab: experiment is Solr-only for now (it reloads cores and "
                         f"reads Solr's cache and GC counters); the cluster is {spec.engine}")
    state = tuning.tuning_state(spec, collection)
    if knob not in state["values"]:
        raise SystemExit(f"searchlab: no knob '{knob}' on {collection} — available: "
                         f"{', '.join(sorted(state['values']))}")
    original = state["values"][knob]
    info = tuning.KNOBS[knob]
    if not info["min"] <= to <= info["max"]:
        raise SystemExit(f"searchlab: {knob} must be between {info['min']} and "
                         f"{info['max']} {info['unit']}")
    if to == original:
        raise SystemExit(f"searchlab: {knob} is already {to:g}; pick a different value")
    if not 1 <= rounds <= 5:
        raise SystemExit("searchlab: rounds must be between 1 and 5")

    def label(v):
        return f"{v:g} {info['unit']}" if v is not None else "solrconfig default"

    # None puts the knob back to solrconfig.xml; a value re-sets an override
    restore = original if _overridden(spec, collection, knob) else None
    configset, shared = _sharing(spec, collection)
    if shared:
        say(f"note: {knob} lives in configset '{configset}', which {', '.join(shared)} "
            f"also use{'s' if len(shared) == 1 else ''}. They see the change and reload "
            "with it, and are put back with it.")

    phases: list[dict] = []
    changed = False

    def phase(name: str, value: float | None) -> None:
        say(f"phase {name} ({knob} = {label(value)}): reloading, "
            f"{warmup:g}s warm-up, then {duration:g}s at {rps:g} rps …")
        time.sleep(settle)
        phases.append({"name": name, "value": value,
                       **measure(spec, collection, rps, warmup, duration, seed, queries_path)})

    # one round is A B A'; more alternate A B A B … A, so every B sits
    # between two A runs and slow drift can't pass for an effect
    names = (["A", "B", "A'"] if rounds == 1 else
             [n for i in range(1, rounds + 1) for n in (f"A{i}", f"B{i}")] + [f"A{rounds + 1}"])
    try:
        _reload(spec, collection)
        phase(names[0], original)
        for b_name, a_name in zip(names[1::2], names[2::2]):
            _set(spec, collection, knob, to)
            changed = True
            _wait_for(spec, collection, knob, to)
            phase(b_name, to)
            _set(spec, collection, knob, restore)
            changed = False
            _wait_for(spec, collection, knob, original)
            phase(a_name, original)
    finally:
        # however the run ends, the cluster goes back to how it was found
        if changed:
            say(f"restoring {knob} to {label(original)}")
            _set(spec, collection, knob, restore)
    return {"collection": collection, "knob": knob, "label": info["label"],
            "unit": info["unit"], "from": original, "to": to, "rps": rps,
            "duration": duration, "warmup": warmup, "seed": seed, "rounds": rounds,
            "phases": phases}


# --------------------------------------------------------------- the report ---

def verdict(a: float | None, b: float | None, a2: float | None, *,
            floor_pct: float = 5.0) -> dict:
    """Is B's difference from A bigger than the gap between A and A'?"""
    if None in (a, b, a2):
        return {"kind": "unknown"}
    base = (a + a2) / 2
    if base == 0:
        return {"kind": "unknown"}
    effect = (b - base) / base * 100
    noise = abs(a - a2) / base * 100
    real = abs(effect) > max(2 * noise, floor_pct)
    return {"kind": "real" if real else "noise", "effect_pct": effect, "noise_pct": noise}


def _pct(v: float) -> str:
    return f"{v:+.1f}%" if abs(v) < 10 else f"{v:+.0f}%"


def verdict_rounds(a_vals: list, b_vals: list, *, floor_pct: float = 5.0) -> dict:
    """Over several rounds: a change counts only if every B run lands on the
    same side of every A run, AND the medians are further apart than the A
    runs are from each other. Being on one side alone isn't enough: in a live
    run, two B runs sat just above three A runs that spread 12%, and the
    "effect" was 9%."""
    if not a_vals or not b_vals or None in a_vals or None in b_vals:
        return {"kind": "unknown"}
    base = statistics.median(a_vals)
    if base == 0:
        return {"kind": "unknown"}
    effect = (statistics.median(b_vals) - base) / base * 100
    noise = (max(a_vals) - min(a_vals)) / base * 100
    apart = min(b_vals) > max(a_vals) or max(b_vals) < min(a_vals)
    return {"kind": "real" if apart and abs(effect) >= max(floor_pct, noise) else "noise",
            "effect_pct": effect, "noise_pct": noise, "apart": apart}


def _format_rounds(res: dict) -> str:
    a_ph = [p for p in res["phases"] if p["name"].startswith("A")]
    b_ph = [p for p in res["phases"] if p["name"].startswith("B")]
    fmt_v = (lambda v: f"{v:g}" if v is not None else "default")
    lines = [
        f"experiment: {res['label']} ({res['knob']}) {fmt_v(res['from'])} -> "
        f"{fmt_v(res['to'])} {res['unit']}, {res['rounds']} rounds",
        f"  {res['collection']}, {res['rps']:g} rps, {res['warmup']:g}s warm-up + "
        f"{res['duration']:g}s measured per phase, seed {res['seed']}, each phase from a reload",
        f"  order: {' '.join(p['name'] for p in res['phases'])}",
        "",
        f"  {'':<22}{'A runs':>20}  {'B runs':>20}   B vs A   A spread",
    ]

    def row(name, key, fmt, judged=False):
        av = [p.get(key) for p in a_ph]
        bv = [p.get(key) for p in b_ph]
        cell = lambda vs: " ".join(fmt(v) if v is not None else "—" for v in vs)  # noqa: E731
        tail = ""
        if judged:
            v = verdict_rounds(av, bv)
            if v["kind"] != "unknown":
                tail = (f"  {v['effect_pct']:+6.1f}%  {v['noise_pct']:6.1f}%   "
                        + ("real" if v["kind"] == "real" else
                           "overlaps A" if not v["apart"] else "too close"))
        lines.append(f"  {name:<22}{cell(av):>20}  {cell(bv):>20} {tail}")

    ms = lambda v: f"{v:.1f}"  # noqa: E731
    row("latency p50 (ms)", "p50_ms", ms, judged=True)
    row("latency p99 (ms)", "p99_ms", ms, judged=True)
    row("  without worst second", "p99_without_worst_s", ms, judged=True)
    row("errors", "errors", lambda v: f"{v:d}")
    row("dropped", "dropped", lambda v: f"{v:d}")
    for cache in CACHES:
        av = [(p.get("hitratio") or {}).get(cache) for p in a_ph]
        bv = [(p.get("hitratio") or {}).get(cache) for p in b_ph]
        if any(v is not None for v in av + bv):
            c = lambda vs: " ".join(f"{v:.2f}" if v is not None else "—" for v in vs)  # noqa: E731
            lines.append(f"  {cache + ' hits':<22}{c(av):>20}  {c(bv):>20}")
    lines.append("")
    n = len(b_ph)
    for name, key in (("p50", "p50_ms"), ("p99", "p99_ms")):
        v = verdict_rounds([p.get(key) for p in a_ph], [p.get(key) for p in b_ph])
        if v["kind"] == "unknown":
            lines.append(_note(f"{name}: no verdict (a phase returned no successful requests)"))
        elif v["kind"] == "real":
            side = "slower" if v["effect_pct"] > 0 else "faster"
            lines.append(_note(f"{name}: all {n} B runs were {side} than every A run "
                         f"(median {_pct(v['effect_pct'])}): a real effect."))
        elif not v["apart"]:
            lines.append(_note(f"{name}: B runs and A runs overlap (median {_pct(v['effect_pct'])}): "
                         "no effect you can rely on."))
        elif abs(v["effect_pct"]) < 5:
            lines.append(_note(f"{name}: B is consistently on one side, but only by "
                         f"{_pct(v['effect_pct'])}: too small to matter."))
        else:
            lines.append(_note(f"{name}: B is consistently on one side ({_pct(v['effect_pct'])}), "
                         f"but by less than the A runs spread among themselves "
                         f"({v['noise_pct']:.0f}%): too close to call. More rounds or "
                         "longer phases would settle it."))
    return "\n".join(lines)


def _note(text: str) -> str:
    # verdicts are sentences; wrapped, they read in a terminal and in the panel
    return textwrap.fill(text, width=100, initial_indent="  ", subsequent_indent="    ")


def format_report(res: dict) -> str:
    if res.get("rounds", 1) > 1:
        return _format_rounds(res)
    ph = {p["name"]: p for p in res["phases"]}
    a, b, a2 = ph.get("A"), ph.get("B"), ph.get("A'")
    p99 = verdict(*(p.get("p99_ms") if p else None for p in (a, b, a2)))
    steady = verdict(*(p.get("p99_without_worst_s") if p else None for p in (a, b, a2)))
    # p99 "moved", but only because of one bad second: the table must say so
    # too, not "real" above a sentence explaining that it isn't
    burst = p99["kind"] == "real" and steady["kind"] == "noise"
    fmt_v = (lambda v: f"{v:g}" if v is not None else "default")
    lines = [
        f"experiment: {res['label']} ({res['knob']}) {fmt_v(res['from'])} -> "
        f"{fmt_v(res['to'])} {res['unit']}",
        f"  {res['collection']}, {res['rps']:g} rps, {res['warmup']:g}s warm-up + "
        f"{res['duration']:g}s measured per phase, seed {res['seed']}, each phase from a reload",
        "",
        f"  {'':<22}{'A':>11}{'B':>11}{'A′':>11}   B vs A      noise (A′ vs A)",
    ]

    def row(name, key, fmt, judged=False, pct=False):
        vals = [p.get(key) if p else None for p in (a, b, a2)]
        cells = "".join(f"{(fmt(v) if v is not None else '—'):>11}" for v in vals)
        tail = ""
        if judged:
            v = verdict(*vals)
            if v["kind"] != "unknown":
                label = ("burst" if key == "p99_ms" and burst else
                         "real" if v["kind"] == "real" else "within noise")
                tail = f"   {v['effect_pct']:+6.1f}%     {v['noise_pct']:5.1f}%   {label}"
        lines.append(f"  {name:<22}{cells}{tail}")

    ms = lambda v: f"{v:.1f}"  # noqa: E731
    row("latency p50 (ms)", "p50_ms", ms, judged=True)
    row("latency p90 (ms)", "p90_ms", ms, judged=True)
    row("latency p99 (ms)", "p99_ms", ms, judged=True)
    row("  without worst second", "p99_without_worst_s", ms, judged=True)
    row("achieved rps", "achieved_rps", lambda v: f"{v:.1f}")
    row("errors", "errors", lambda v: f"{v:d}")
    row("dropped", "dropped", lambda v: f"{v:d}")
    row("GC pauses", "gc_pauses", lambda v: f"{v:d}")
    row("GC time (ms)", "gc_ms", lambda v: f"{v:d}")
    for cache in CACHES:
        vals = [((p or {}).get("hitratio") or {}).get(cache) for p in (a, b, a2)]
        if any(v is not None for v in vals):
            lines.append(f"  {cache + ' hits':<22}"
                         + "".join(f"{(f'{v:.2f}' if v is not None else '—'):>11}" for v in vals))

    lines.append("")
    p50 = verdict(*(p.get("p50_ms") if p else None for p in (a, b, a2)))
    if p50["kind"] == "unknown":
        lines.append("  no verdict: a phase returned no successful requests")
    else:
        for name, v in (("p50", p50), ("p99", p99)):
            if name == "p99" and burst:
                src = max((p for p in (a, b, a2) if p),
                          key=lambda p: (p.get("p99_ms") or 0) - (p.get("p99_without_worst_s") or 0))
                lines.append(_note(
                    f"p99: B moved {_pct(v['effect_pct'])}, but it came from one burst: "
                    f"{src.get('worst_slow')} of phase {src['name']}'s slowest requests fell in "
                    f"the second starting at {src.get('worst_s')}s. Leave each phase's worst "
                    f"second out and B moves {_pct(steady['effect_pct'])}, within the "
                    f"{steady['noise_pct']:.0f}% noise. A stall (GC, a merge, a reload "
                    "elsewhere), not the setting."))
            elif v["kind"] == "real":
                lines.append(_note(f"{name}: B moved {_pct(v['effect_pct'])}, more than twice the "
                             f"{v['noise_pct']:.1f}% that A and A′ differ by: likely a real effect."))
            elif abs(v["effect_pct"]) < 5 and v["noise_pct"] < 5:
                lines.append(f"  {name}: B moved {_pct(v['effect_pct'])}: no measurable change.")
            else:
                lines.append(_note(f"{name}: B moved {_pct(v['effect_pct'])}, but A and A′ (same "
                             f"setting) differ by {v['noise_pct']:.1f}%. Can't tell it from noise."))
        # a cache that clearly moved, read against whether latency did: the
        # likely mechanism when it did, and a cheap miss when it didn't
        moved_latency = p50["kind"] == "real" or (p99["kind"] == "real" and not burst)
        for cache in CACHES:
            ha, hb, ha2 = (((p or {}).get("hitratio") or {}).get(cache) for p in (a, b, a2))
            if None in (ha, hb, ha2) or abs(hb - ha) < 0.05 or abs(ha2 - ha) >= 0.03:
                continue
            way = "fell" if hb < ha else "rose"
            lines.append(_note(
                f"{cache} hit ratio {way} from {ha:.2f} to {hb:.2f}"
                + (", the likely mechanism." if moved_latency else
                   ", and latency didn't notice: on this index and workload, a miss costs "
                   "about what a hit does.")))
    lines.append("  (one A/A′ pair is a rough measure of noise; run it again before "
                 "acting on a small effect)")
    n = min((p["requests"] for p in res["phases"]), default=0)
    if n < 1000:
        lines.append(f"  (p99 over {n} requests is roughly the {max(n // 100, 1)}"
                     f"{'st' if max(n // 100, 1) == 1 else 'th'} slowest; "
                     "longer phases make it steadier)")
    return "\n".join(lines)
