"""CI ergonomics: assertion gates over load reports and human-friendly values.

`--assert "p99_ms<50"` turns any load, replay, or drill into a regression
gate: the command exits non-zero when the assertion fails, so a CI job can
run `searchlab load ... --assert "p99_ms<100" --assert "errors=0"` and fail the
build when latency regresses. Assertions evaluate against the same metric
names the JSON report uses.
"""

from __future__ import annotations

import operator
import re
import sys

_OPS = {
    "<=": operator.le, ">=": operator.ge,
    "<": operator.lt, ">": operator.gt,
    "==": operator.eq, "=": operator.eq, "!=": operator.ne,
}
_ASSERT = re.compile(r"^\s*(?P<key>[a-z0-9_.]+)\s*(?P<op><=|>=|==|!=|<|>|=)\s*(?P<val>[\d.]+)\s*$")

METRIC_KEYS = ("requests", "errors", "dropped", "achieved_rps",
               "p50_ms", "p90_ms", "p99_ms", "p999_ms")


def parse_assertion(expr: str) -> tuple[str, str, float]:
    m = _ASSERT.match(expr)
    if not m:
        sys.exit(f"searchlab: bad assertion '{expr}' — expected e.g. \"p99_ms<50\", "
                 f"\"errors=0\" (metrics: {', '.join(METRIC_KEYS)})")
    key = m.group("key")
    if key not in METRIC_KEYS:
        sys.exit(f"searchlab: unknown metric '{key}' in assertion — "
                 f"valid: {', '.join(METRIC_KEYS)}")
    return key, m.group("op"), float(m.group("val"))


def check_assertions(report: dict, exprs: list[str]) -> list[str]:
    """Returns failure messages; empty list means all assertions hold."""
    failures = []
    for expr in exprs:
        key, op, val = parse_assertion(expr)
        actual = report.get(key)
        if actual is None:
            failures.append(f"{expr}: metric '{key}' missing from report")
        elif not _OPS[op](actual, val):
            failures.append(f"{expr}: actual {key} = {actual}")
    return failures


def result_metrics(result) -> dict:
    """The same metric dict save_report writes, computed from a LoadResult."""
    return {
        "requests": len(result.records),
        "errors": sum(1 for r in result.records if not r.ok),
        "dropped": result.dropped,
        "achieved_rps": round(len(result.records) / result.duration, 1)
        if result.duration else 0,
        "p50_ms": round(result.percentile(50), 2),
        "p90_ms": round(result.percentile(90), 2),
        "p99_ms": round(result.percentile(99), 2),
        "p999_ms": round(result.percentile(99.9), 2),
    }


# ---------------------------------------------------- human-friendly units ---

_DURATION = re.compile(r"^\s*(?:(?P<h>\d+(?:\.\d+)?)h)?\s*(?:(?P<m>\d+(?:\.\d+)?)m)?"
                       r"\s*(?:(?P<s>\d+(?:\.\d+)?)s?)?\s*$")


def parse_duration(value: str | float) -> float:
    """'90', '90s', '2m', '1h30m', '2m30s' -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION.match(value)
    if not m or not any(m.groupdict().values()):
        sys.exit(f"searchlab: bad duration '{value}' — try 90, 90s, 2m, 1h30m")
    h, mi, s = (float(m.group(g) or 0) for g in ("h", "m", "s"))
    return h * 3600 + mi * 60 + s


def parse_count(value: str | int) -> int:
    """'10000', '10k', '1.5m' -> int."""
    if isinstance(value, int):
        return value
    v = value.strip().lower().replace("_", "").replace(",", "")
    mult = 1
    if v.endswith("k"):
        mult, v = 1_000, v[:-1]
    elif v.endswith("m"):
        mult, v = 1_000_000, v[:-1]
    try:
        return int(float(v) * mult)
    except ValueError:
        sys.exit(f"searchlab: bad count '{value}' — try 10000, 10k, 1.5m")
