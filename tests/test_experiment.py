"""experiment: the A/B/A verdict, burst detection, and that the knob is always
put back exactly as it was found."""

from __future__ import annotations

import random

import pytest

import searchlab.experiment as xp
from searchlab.cluster import ClusterSpec
from searchlab.loadtest import RequestRecord

# ---------------------------------------------------------------- verdict ---


def test_verdict_needs_more_than_the_gap_between_a_and_a_prime():
    assert xp.verdict(10.0, 13.0, 10.2)["kind"] == "real"        # +29%, noise 2%
    assert xp.verdict(10.0, 13.0, 12.0)["kind"] == "noise"       # +18%, noise 18%
    assert xp.verdict(10.0, 10.3, 10.0)["kind"] == "noise"       # +3%: under the 5% floor
    assert xp.verdict(None, 1.0, 1.0)["kind"] == "unknown"
    assert xp.verdict(0.0, 1.0, 0.0)["kind"] == "unknown"


def _phase(name, stall=False, seed=1):
    rng = random.Random(seed)
    recs = []
    for i in range(1800):                       # 30s at 60 rps, after a 15s warm-up
        t = 15 + i / 60
        lat = max(rng.gauss(6.5, 1.2), 1.0)
        if stall and 27 <= t < 28:
            lat = 350 + rng.random() * 50
        recs.append(RequestRecord(t, lat, 200, "q", True))
    lats = sorted(r.latency_ms for r in recs)

    def pc(p):
        return lats[min(int(len(lats) * p / 100), len(lats) - 1)]

    return {"name": name, "value": 64 if name == "B" else 512, "requests": len(recs),
            "errors": 0, "dropped": 0, "achieved_rps": 60.0, "p50_ms": pc(50),
            "p90_ms": pc(90), "p99_ms": pc(99), "gc_ms": 10, "gc_pauses": 1,
            "hitratio": {"queryResultCache": 0.66 if name != "B" else 0.54},
            **xp.worst_second(recs, 15)}


def _result(phases):
    return {"collection": "c", "knob": "result_cache", "label": "Result cache size",
            "unit": "entries", "from": 512, "to": 64, "rps": 60, "duration": 30,
            "warmup": 15, "seed": 7, "phases": phases}


def test_one_stall_in_b_is_reported_as_a_burst_not_an_effect():
    # This is the shape of a real false positive: a one-off stall in B made
    # p99 jump 36x; running the same experiment again, it didn't happen.
    out = xp.format_report(_result([_phase("A"), _phase("B", stall=True, seed=2),
                                    _phase("A'", seed=3)]))
    assert "came from one burst" in out and "phase B" in out and "starting at 12s" in out
    assert "likely a real effect" not in out
    assert "queryResultCache hits" in out


def test_steady_shift_is_reported_as_likely_real():
    a, a2 = _phase("A"), _phase("A'", seed=3)
    b = _phase("B", seed=2)
    for k in ("p50_ms", "p90_ms", "p99_ms", "p99_without_worst_s"):
        b[k] *= 1.5                               # everything 50% slower, evenly
    out = xp.format_report(_result([a, b, a2]))
    assert "p50: B moved +50%" in out and "likely a real effect" in out
    assert "burst" not in out


def test_no_change_is_said_plainly():
    out = xp.format_report(_result([_phase("A"), _phase("B", seed=2), _phase("A'", seed=3)]))
    assert "no measurable change" in out
    # the cache clearly moved and latency didn't: say what that means
    assert "queryResultCache hit ratio fell from 0.66 to 0.54" in out
    assert "latency didn't notice" in out


def test_worst_second_needs_enough_requests():
    assert xp.worst_second([RequestRecord(1.0, 5.0, 200, "q", True)] * 50, 0) == {}


# ------------------------------------------------------- the run's shape ---


@pytest.fixture
def fake_cluster(monkeypatch):
    calls = []
    state = {"filter_cache": 512.0}

    monkeypatch.setattr(xp.tuning, "tuning_state",
                        lambda spec, coll: {"values": dict(state), "registry": {}})
    monkeypatch.setattr(xp.tuning, "read_tuning", lambda spec, coll: dict(state))
    monkeypatch.setattr(xp, "_reload", lambda spec, coll: calls.append(("reload",)))
    monkeypatch.setattr(xp, "_sharing", lambda spec, coll: ("searchlab", ["other"]))

    def fake_set(spec, coll, knob, value):
        calls.append(("set", value))
        state[knob] = 512.0 if value is None else value   # None: back to the file's 512

    monkeypatch.setattr(xp, "_set", fake_set)
    monkeypatch.setattr(xp, "measure", lambda *a, **k: (
        calls.append(("measure", state["filter_cache"])) or {"requests": 10}))
    monkeypatch.setattr(xp.time, "sleep", lambda s: None)
    return calls, state


def test_a_b_a_order_and_the_knob_goes_back_to_the_file(fake_cluster, monkeypatch):
    calls, state = fake_cluster
    monkeypatch.setattr(xp, "_overridden", lambda spec, coll, knob: False)
    said = []
    res = xp.run_experiment(ClusterSpec(), "c", "filter_cache", 64, say=said.append)
    assert calls == [("reload",), ("measure", 512.0), ("set", 64), ("measure", 64),
                     ("set", None), ("measure", 512.0)]
    # not overridden at the start, so it's unset rather than re-set to 512:
    # writing 512 would leave an override that wasn't there before
    assert [p["name"] for p in res["phases"]] == ["A", "B", "A'"]
    assert any("also uses" in s and "other" in s for s in said)


def test_an_existing_override_is_restored_as_an_override(fake_cluster, monkeypatch):
    calls, state = fake_cluster
    monkeypatch.setattr(xp, "_overridden", lambda spec, coll, knob: True)
    xp.run_experiment(ClusterSpec(), "c", "filter_cache", 64, say=lambda s: None)
    assert ("set", 512.0) in calls and ("set", None) not in calls


def test_knob_is_restored_when_a_phase_fails(fake_cluster, monkeypatch):
    calls, state = fake_cluster
    monkeypatch.setattr(xp, "_overridden", lambda spec, coll, knob: False)

    def boom(*a, **k):
        if state["filter_cache"] == 64:
            raise KeyboardInterrupt
        return {"requests": 10}

    monkeypatch.setattr(xp, "measure", boom)
    with pytest.raises(KeyboardInterrupt):
        xp.run_experiment(ClusterSpec(), "c", "filter_cache", 64, say=lambda s: None)
    assert calls[-1] == ("set", None) and state["filter_cache"] == 512.0


def test_refusals_before_anything_changes(fake_cluster):
    calls, _ = fake_cluster
    with pytest.raises(SystemExit, match="Solr-only"):
        xp.run_experiment(ClusterSpec(engine="opensearch"), "c", "filter_cache", 64)
    with pytest.raises(SystemExit, match="available: filter_cache"):
        xp.run_experiment(ClusterSpec(), "c", "nope", 1)
    with pytest.raises(SystemExit, match="already 512"):
        xp.run_experiment(ClusterSpec(), "c", "filter_cache", 512)
    with pytest.raises(SystemExit, match="between"):
        xp.run_experiment(ClusterSpec(), "c", "filter_cache", 10**9)
    assert calls == []
