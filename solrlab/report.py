"""Load-report comparison and self-contained HTML reports.

`compare` is the version-regression workflow: run the identical seeded
gen/index/load sequence against two Solr versions, then diff the reports.
`html_report` renders a report (or a comparison) as a single dependency-free
HTML file with inline SVG charts — safe to attach to a ticket or email.
"""

from __future__ import annotations

import json
from pathlib import Path

_METRICS = [
    ("requests", "requests", 0),
    ("errors", "errors", 0),
    ("dropped", "dropped", 0),
    ("achieved rps", "achieved_rps", 1),
    ("p50 ms", "p50_ms", 1),
    ("p90 ms", "p90_ms", 1),
    ("p99 ms", "p99_ms", 1),
    ("p99.9 ms", "p999_ms", 1),
]


def load_report(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text())
    if "achieved_rps" not in data and data.get("duration_s"):
        data["achieved_rps"] = round(data["requests"] / data["duration_s"], 1)
    return data


def compare_text(path_a: str | Path, path_b: str | Path) -> str:
    a, b = load_report(path_a), load_report(path_b)
    name_a, name_b = Path(path_a).stem, Path(path_b).stem
    w = max(len(name_a), len(name_b), 10)
    lines = [f"{'metric':<14} {name_a:>{w}} {name_b:>{w}} {'delta':>10}"]
    for label, key, nd in _METRICS:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            continue
        delta = vb - va
        pct = f" ({delta / va * 100:+.0f}%)" if va else ""
        lines.append(
            f"{label:<14} {round(va, nd):>{w}} {round(vb, nd):>{w}} "
            f"{round(delta, nd):>+10}{pct}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------- html ---

def _svg_timeline(timeline: list[dict], width: int = 760, height: int = 220,
                  events: list[dict] | None = None) -> str:
    """Inline SVG: p50/p99 lines over time plus an rps area underneath.
    `events` ({at_s, action, node}) render as annotated vertical markers."""
    if not timeline:
        return "<p>No timeline data.</p>"
    pad = 40
    xs = [row["t"] for row in timeline]
    x_max = max(xs) or 1
    y_max = max(max(r["p99_ms"] for r in timeline), 1)
    rps_max = max(max(r["rps"] for r in timeline), 1)

    def px(t: float) -> float:
        return pad + (t / x_max) * (width - 2 * pad)

    def py(ms: float) -> float:
        return height - pad - (ms / y_max) * (height - 2 * pad)

    def pr(rps: float) -> float:
        return height - pad - (rps / rps_max) * (height - 2 * pad) * 0.5

    def line(key, color):
        pts = " ".join(f"{px(r['t']):.1f},{py(r[key]):.1f}" for r in timeline)
        return f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>'

    rps_pts = " ".join(f"{px(r['t']):.1f},{pr(r['rps']):.1f}" for r in timeline)
    rps_area = (
        f'<polyline fill="none" stroke="#bbb" stroke-width="1.5" '
        f'stroke-dasharray="4 3" points="{rps_pts}"/>'
    )
    err_marks = "".join(
        f'<circle cx="{px(r["t"]):.1f}" cy="{py(r["p99_ms"]):.1f}" r="4" fill="#d33"/>'
        for r in timeline
        if r.get("errors")
    )
    grid = "".join(
        f'<line x1="{pad}" y1="{py(y_max * f):.1f}" x2="{width - pad}" y2="{py(y_max * f):.1f}" stroke="#eee"/>'
        f'<text x="4" y="{py(y_max * f) + 4:.1f}" font-size="10" fill="#888">{y_max * f:.0f}ms</text>'
        for f in (0.25, 0.5, 0.75, 1.0)
    )
    ev_marks = ""
    for i, e in enumerate(events or []):
        x = px(e["at_s"])
        y_lab = 22 + (i % 3) * 13  # stagger labels so close events stay legible
        ev_marks += (
            f'<line x1="{x:.1f}" y1="16" x2="{x:.1f}" y2="{height - pad}" '
            f'stroke="#8040a0" stroke-width="1.5" stroke-dasharray="5 4"/>'
            f'<text x="{x + 4:.1f}" y="{y_lab}" font-size="10" fill="#8040a0">'
            f'{e["action"]} {e["node"]}</text>'
        )
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{grid}{rps_area}{line("p50_ms", "#2a7")}{line("p99_ms", "#e80")}{err_marks}{ev_marks}'
        f'<text x="{pad}" y="14" font-size="11" fill="#2a7">p50</text>'
        f'<text x="{pad + 34}" y="14" font-size="11" fill="#e80">p99</text>'
        f'<text x="{pad + 68}" y="14" font-size="11" fill="#999">rps (dashed)</text>'
        f'<text x="{width - pad - 20}" y="{height - 8}" font-size="10" fill="#888">{x_max:.0f}s</text>'
        "</svg>"
    )


def _histogram_bars(hist: list[dict]) -> str:
    if not hist:
        return "<p>No histogram data (older report format).</p>"
    total = sum(b["count"] for b in hist) or 1
    rows = []
    for b in hist:
        label = f"&le; {b['le_ms']} ms" if b["le_ms"] is not None else f"&gt; {b['gt_ms']} ms"
        pct = b["count"] / total * 100
        rows.append(
            f"<tr><td>{label}</td>"
            f"<td style='min-width:280px'><div style='background:#2B5EA7;height:10px;"
            f"width:{max(pct, 0.5):.1f}%'></div></td>"
            f"<td>{b['count']} ({pct:.1f}%)</td></tr>"
        )
    return f"<table>{''.join(rows)}</table>"


def _summary_table(report: dict) -> str:
    rows = "".join(
        f"<tr><td>{label}</td><td>{report.get(key, '—')}</td></tr>"
        for label, key, _ in _METRICS
        if report.get(key) is not None
    )
    return f"<table>{rows}</table>"


_STYLE = """
body{font-family:-apple-system,Segoe UI,sans-serif;max-width:860px;margin:2rem auto;
padding:0 1rem;color:#222}h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}
table{border-collapse:collapse;margin:.5rem 0}td{border:1px solid #ddd;padding:.3rem .8rem}
td:first-child{color:#666}code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}
"""


def html_report(report_path: str | Path, out: str | Path, title: str = "solrlab load report") -> None:
    report = load_report(report_path)
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{_STYLE}</style></head><body><h1>{title}</h1>"
        f"<p><code>{Path(report_path).name}</code> — target {report.get('target_rps')} rps, "
        f"{report.get('duration_s')}s</p>"
        f"<h2>Summary</h2>{_summary_table(report)}"
        f"<h2>Latency over time</h2>{_svg_timeline(report.get('timeline', []))}"
        f"<h2>Latency distribution</h2>{_histogram_bars(report.get('histogram', []))}"
        "<p style='color:#888;font-size:.85rem'>Red dots mark intervals containing errors. "
        "Open-loop schedule: latency spikes are real, not client back-pressure.</p>"
        "</body></html>"
    )
    Path(out).write_text(html)


_GC_COLORS = {"Young": "#2B5EA7", "Remark": "#2F7D51", "Cleanup": "#7A8B57",
              "Initial Mark": "#6B4FA0", "Full": "#C2402F"}


def _svg_gc_timeline(pauses, width: int = 760, height: int = 240) -> str:
    """Pause scatter over JVM uptime: square-root y-scale keeps 10ms young
    pauses visible next to an 800ms Full GC; Fulls get an annotated marker."""
    if not pauses:
        return "<p>No pauses.</p>"
    pad = 44
    t0, t1 = pauses[0].uptime_s, pauses[-1].uptime_s or 1
    span = (t1 - t0) or 1
    y_max = max(p.ms for p in pauses)

    def px(t):
        return pad + (t - t0) / span * (width - 2 * pad)

    def py(ms):
        return height - pad - (ms / y_max) ** 0.5 * (height - 2 * pad)

    grid = "".join(
        f'<line x1="{pad}" y1="{py(y_max * f):.1f}" x2="{width - pad}" y2="{py(y_max * f):.1f}" stroke="#eee"/>'
        f'<text x="4" y="{py(y_max * f) + 4:.1f}" font-size="10" fill="#888">{y_max * f:.0f}ms</text>'
        for f in (0.04, 0.25, 1.0)
    )
    dots, fulls = "", ""
    for p in pauses:
        c = _GC_COLORS.get(p.kind, "#666")
        if p.kind == "Full":
            fulls += (
                f'<line x1="{px(p.uptime_s):.1f}" y1="16" x2="{px(p.uptime_s):.1f}" '
                f'y2="{height - pad}" stroke="{c}" stroke-width="1" stroke-dasharray="3 3"/>'
                f'<circle cx="{px(p.uptime_s):.1f}" cy="{py(p.ms):.1f}" r="5" fill="{c}"/>'
                f'<text x="{px(p.uptime_s) + 6:.1f}" y="{py(p.ms) - 6:.1f}" font-size="10" '
                f'fill="{c}">Full {p.ms:.0f}ms</text>'
            )
        else:
            dots += f'<circle cx="{px(p.uptime_s):.1f}" cy="{py(p.ms):.1f}" r="2.5" fill="{c}" fill-opacity=".75"/>'
    legend = "".join(
        f'<text x="{pad + i * 90}" y="12" font-size="10" fill="{c}">&#9679; {k}</text>'
        for i, (k, c) in enumerate(_GC_COLORS.items())
    )
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{grid}{dots}{fulls}{legend}'
        f'<text x="{pad}" y="{height - 8}" font-size="10" fill="#888">uptime {t0:.0f}s</text>'
        f'<text x="{width - pad - 40}" y="{height - 8}" font-size="10" fill="#888">{t1:.0f}s</text>'
        "</svg>"
    )


_SWEEP_COLS = [
    ("p50 ms", "p50_ms", True), ("p99 ms", "p99_ms", True),
    ("p99.9 ms", "p999_ms", True), ("errors", "errors", True),
    ("dropped", "dropped", True), ("achieved rps", "achieved_rps", False),
]


def html_sweep(results: list[dict], out: str | Path,
               title: str = "solrlab sweep") -> None:
    """Config-matrix comparison; the best cell per metric is highlighted
    (lower is better except achieved rps)."""
    best: dict[str, float] = {}
    for _, key, lower in _SWEEP_COLS:
        vals = [r["metrics"].get(key) for r in results if r["metrics"].get(key) is not None]
        if vals:
            best[key] = min(vals) if lower else max(vals)
    idx_vals = [r["index_docs_per_s"] for r in results if r.get("index_docs_per_s")]
    show_idx = bool(idx_vals)
    header = "<tr><td>config</td>" + "".join(
        f"<td>{label}</td>" for label, _, _ in _SWEEP_COLS
    ) + ("<td>index docs/s</td>" if show_idx else "") + "</tr>"
    rows = []
    for r in results:
        tds = [f"<td><b>{r['name']}</b></td>"]
        for _, key, _ in _SWEEP_COLS:
            v = r["metrics"].get(key)
            mark = " style='background:#dcefdc;font-weight:600'" \
                if v is not None and v == best.get(key) else ""
            tds.append(f"<td{mark}>{v if v is not None else '&mdash;'}</td>")
        if show_idx:
            v = r.get("index_docs_per_s")
            mark = " style='background:#dcefdc;font-weight:600'" \
                if v is not None and idx_vals and v == max(idx_vals) else ""
            tds.append(f"<td{mark}>{v if v is not None else '&mdash;'}</td>")
        rows.append("<tr>" + "".join(tds) + "</tr>")
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{_STYLE}</style></head><body><h1>{title}</h1>"
        f"<p>{len(results)} configuration(s), identical seeded workload; "
        "green = best per column.</p>"
        f"<table>{header}{''.join(rows)}</table>"
        "</body></html>"
    )
    Path(out).write_text(html)


def html_gc(pauses_by_node: dict, out: str | Path, title: str = "solrlab gc report") -> None:
    """Self-contained GC report: per-node pause timeline + text summary."""
    from .gclog import summarize

    sections = "".join(
        f"<h2>{node}</h2>{_svg_gc_timeline(pauses)}"
        f"<pre>{summarize(pauses, label=node)}</pre>"
        for node, pauses in pauses_by_node.items()
    )
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{_STYLE}pre{{background:#f7f7f5;border:1px solid #ddd;padding:.8rem;"
        f"font-size:.8rem;overflow-x:auto}}</style></head><body><h1>{title}</h1>"
        "<p style='color:#888;font-size:.85rem'>Square-root vertical scale — small young "
        "pauses stay visible next to Full GCs. Dashed verticals mark Full collections.</p>"
        f"{sections}</body></html>"
    )
    Path(out).write_text(html)


def html_drill(report_path: str | Path, out: str | Path, title: str = "solrlab drill") -> None:
    """Self-contained drill report: annotated timeline, summary, latency
    histogram, and the before/after metrics diff."""
    report = load_report(report_path)
    events = report.get("events", [])
    ev_rows = "".join(
        f"<tr><td>t={e['at_s']}s</td><td>{e['action']}</td><td>{e['node']}</td></tr>"
        for e in events
    ) or "<tr><td colspan=3>none</td></tr>"
    diff = report.get("metrics_diff", "")
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{_STYLE}pre{{background:#f7f7f5;border:1px solid #ddd;padding:.8rem;"
        f"font-size:.8rem;overflow-x:auto}}</style></head><body><h1>{title}</h1>"
        f"<p><code>{Path(report_path).name}</code> — target {report.get('target_rps')} rps, "
        f"{report.get('duration_s')}s, {len(events)} fault(s) injected</p>"
        f"<h2>Timeline · faults annotated</h2>"
        f"{_svg_timeline(report.get('timeline', []), events=events)}"
        "<p style='color:#888;font-size:.85rem'>Green p50, orange p99, dashed grey rps, "
        "red dots = intervals with errors, purple dashed verticals = injected faults.</p>"
        f"<h2>Faults</h2><table><tr><td>when</td><td>action</td><td>node</td></tr>{ev_rows}</table>"
        f"<h2>Summary</h2>{_summary_table(report)}"
        f"<h2>Latency distribution</h2>{_histogram_bars(report.get('histogram', []))}"
        + (f"<h2>Metrics before &rarr; after</h2><pre>{diff}</pre>" if diff else "")
        + "</body></html>"
    )
    Path(out).write_text(html)


def html_compare(path_a: str | Path, path_b: str | Path, out: str | Path) -> None:
    a, b = load_report(path_a), load_report(path_b)
    name_a, name_b = Path(path_a).stem, Path(path_b).stem
    rows = []
    for label, key, nd in _METRICS:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            continue
        delta = round(vb - va, nd)
        color = "#c33" if ("ms" in label or label in ("errors", "dropped")) and delta > 0 else "#282"
        rows.append(
            f"<tr><td>{label}</td><td>{round(va, nd)}</td><td>{round(vb, nd)}</td>"
            f"<td style='color:{color}'>{delta:+}</td></tr>"
        )
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>solrlab compare</title>"
        f"<style>{_STYLE}</style></head><body><h1>solrlab: {name_a} vs {name_b}</h1>"
        f"<table><tr><td>metric</td><td>{name_a}</td><td>{name_b}</td><td>delta</td></tr>"
        f"{''.join(rows)}</table>"
        f"<h2>{name_a}</h2>{_svg_timeline(a.get('timeline', []))}"
        f"<h2>{name_b}</h2>{_svg_timeline(b.get('timeline', []))}"
        "</body></html>"
    )
    Path(out).write_text(html)
