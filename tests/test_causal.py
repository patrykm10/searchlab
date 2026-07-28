"""The causal timeline: linking a cause to the effect it produced."""

from __future__ import annotations

from searchlab.causal import CausalTimeline, classify_log_event


def _node(gc_ms: float, ts: float, util=0.1, queued=0):
    return {
        "ts": ts,
        "jvm": {"heap_used_mb": 500, "heap_max_mb": 1024,
                "gc": {"G1-Young-Generation": {"count": 5, "time": gc_ms}}},
        "threads": {"utilization": util, "queued": queued, "size": 10},
        "cores": {},
    }


# ------------------------------------------------------- log classification --

def test_recognizes_the_middle_of_the_chain():
    """ZK session loss and replica-down are reported only in logs, which is
    why the log stream has to feed the timeline."""
    kind, _, _ = classify_log_event(
        "solr1 | 2026-01-01 00:00:00.000 WARN  Our previous ZooKeeper session was expired")
    assert kind == "zk_lost"
    kind, _, _ = classify_log_event(
        "solr2 | 2026-01-01 00:00:00.000 INFO  Setting shard1 state to down")
    assert kind == "replica_down"
    assert classify_log_event("solr1 | INFO  ordinary chatter") is None


# ------------------------------------------------------------------ linking --

def test_effect_links_back_to_its_cause():
    t = CausalTimeline()
    gc = t.add("gc_pause", "paused 2.3s", "…", ts=1000.0)
    zk = t.add("zk_lost", "session lost", "…", ts=1000.4)
    down = t.add("replica_down", "replica down", "…", ts=1001.2)
    assert zk["caused_by"] == gc["seq"]
    assert down["caused_by"] == zk["seq"]


def test_a_distant_event_is_not_blamed_on_an_old_cause():
    """Linking anything that merely happened later would invent causality."""
    t = CausalTimeline()
    t.add("gc_pause", "paused", "…", ts=1000.0)
    late = t.add("replica_down", "replica down", "…", ts=1000.0 + 600)
    assert late["caused_by"] is None


def test_chain_order_is_respected_not_just_recency():
    """A cause must precede its effect in the chain, not merely in time."""
    t = CausalTimeline()
    t.add("queries_failing", "queries failing", "…", ts=1000.0)
    gc = t.add("gc_pause", "paused", "…", ts=1000.5)
    # gc_pause is the first link, so nothing can have caused it
    assert gc["caused_by"] is None


def test_chains_group_linked_events_and_skip_lone_ones():
    t = CausalTimeline()
    t.add("gc_pause", "paused", "…", ts=1000.0)
    t.add("zk_lost", "session lost", "…", ts=1000.3)
    t.add("recovery", "recovering", "…", ts=2000.0)   # unrelated, unlinked
    chains = t.chains()
    assert len(chains) == 1                     # the lone event is not a story
    assert [e["kind"] for e in chains[0]] == ["gc_pause", "zk_lost"]


# --------------------------------------------------- derived from snapshots --

def test_long_gc_pause_becomes_an_event():
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node(gc_ms=100, ts=1000.0)})   # baseline only
    assert t.recent() == []
    # 2.5s of GC across a 5s interval
    t.observe_snapshot({"solr1": _node(gc_ms=2600, ts=1005.0)})
    events = t.recent()
    assert len(events) == 1
    assert events[0]["kind"] == "gc_pause"
    assert events[0]["seconds"] == 2.5
    assert events[0]["node"] == "solr1"


def test_ordinary_gc_is_not_reported_as_an_incident():
    """Young-gen collections happen constantly; flagging them would bury the
    pauses that actually break things."""
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node(gc_ms=100, ts=1000.0)})
    t.observe_snapshot({"solr1": _node(gc_ms=140, ts=1005.0)})   # 40ms in 5s
    assert t.recent() == []


def test_thread_pool_saturation_is_detected_and_does_not_repeat():
    t = CausalTimeline()
    busy = {"solr1": _node(gc_ms=0, ts=1000.0, util=1.0, queued=7)}
    t.observe_snapshot(busy)
    t.observe_snapshot({"solr1": _node(gc_ms=0, ts=1002.0, util=1.0, queued=9)})
    sat = [e for e in t.recent() if e["kind"] == "threads_saturated"]
    assert len(sat) == 1              # a persisting condition fires once
    assert sat[0]["queued"] == 7


def test_saturation_refires_after_it_clears():
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node(gc_ms=0, ts=1000.0, util=1.0, queued=3)})
    t.observe_snapshot({"solr1": _node(gc_ms=0, ts=1002.0, util=0.2, queued=0)})
    t.observe_snapshot({"solr1": _node(gc_ms=0, ts=1004.0, util=1.0, queued=5)})
    sat = [e for e in t.recent() if e["kind"] == "threads_saturated"]
    assert len(sat) == 2


def test_busy_threads_without_a_queue_are_not_saturation():
    """Fully utilised with nothing waiting is a healthy, well-sized pool."""
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node(gc_ms=0, ts=1000.0, util=1.0, queued=0)})
    assert [e for e in t.recent() if e["kind"] == "threads_saturated"] == []


def test_client_side_failures_are_recorded():
    t = CausalTimeline()
    t.observe_snapshot({}, loadtest={"errors": 0, "dropped": 0})
    assert t.recent() == []
    t.observe_snapshot({}, loadtest={"errors": 3, "dropped": 40})
    ev = t.recent()[-1]
    assert ev["kind"] == "queries_failing"
    assert ev["dropped"] == 40


def _node_with_handler(errors, timeouts, ts=1000.0):
    n = _node(gc_ms=0, ts=ts)
    n["cores"] = {"c": {"handler": {"errors": errors, "timeouts": timeouts}}}
    return n


def test_drops_with_no_solr_errors_are_named_as_slowness_not_rejection():
    """Observed live: 44k client drops against 0 Solr errors. Saying the
    cluster 'dropped queries' there would point at the wrong fix."""
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node_with_handler(0, 0)},
                       loadtest={"errors": 0, "dropped": 900, "ts": 1000.0})
    detail = t.recent()[-1]["detail"]
    assert "rejected none" in detail
    assert "too slow" in detail


def test_drops_alongside_solr_errors_are_named_as_rejection():
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node_with_handler(12, 4)},
                       loadtest={"errors": 5, "dropped": 900, "ts": 1000.0})
    detail = t.recent()[-1]["detail"]
    assert "actively rejected" in detail


def test_unreachable_node_is_skipped_not_crashed_on():
    t = CausalTimeline()
    t.observe_snapshot({"solr1": {"error": "timeout"}})
    assert t.recent() == []


def test_full_incident_reads_as_one_sequence():
    """The point of the feature: four separate signals become one story."""
    t = CausalTimeline()
    t.observe_snapshot({"solr1": _node(gc_ms=0, ts=1000.0)})
    t.observe_snapshot({"solr1": _node(gc_ms=2400, ts=1002.0)})     # GC pause
    t.add("zk_lost", "ZooKeeper session lost", "…", node="solr1", ts=1002.3)
    t.add("replica_down", "Replica marked down", "…", node="solr1", ts=1003.1)
    # the load-test block carries the same clock as the snapshot it came with
    t.observe_snapshot({"solr1": _node(gc_ms=2400, ts=1004.0)},
                       loadtest={"errors": 12, "dropped": 88, "ts": 1004.0})

    chain = t.chains()[0]
    assert [e["kind"] for e in chain] == [
        "gc_pause", "zk_lost", "replica_down", "queries_failing"]
    # and it reads in time order, cause first
    assert chain == sorted(chain, key=lambda e: e["ts"])
