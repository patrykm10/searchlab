"""Learn engine and explain tests: scripted lesson runs, condition math,
built-in lesson validation, debug-output translation."""

from __future__ import annotations

import pytest

from searchlab.explain import explain_report, format_explain, format_timing
from searchlab.learn import (builtin_lessons, check_condition, dig, load_lesson, render,
                             run_lesson)

# -------------------------------------------------------------- conditions ---

def test_dig_paths():
    body = {"cluster": {"live_nodes": ["a", "b"], "collections": {"c": {"health": "GREEN"}}}}
    assert dig(body, "cluster.live_nodes") == ["a", "b"]
    assert dig(body, "cluster.collections.c.health") == "GREEN"
    assert dig(body, "cluster.live_nodes.0") == "a"
    assert dig(body, "cluster.nope.deep") is None


def test_conditions():
    body = {"cluster": {"live_nodes": ["a", "b"]}, "response": {"numFound": 1}}
    assert check_condition(body, {"path": "cluster.live_nodes", "op": "len_eq", "value": 2})
    assert check_condition(body, {"path": "cluster.live_nodes", "op": "len_gte", "value": 1})
    assert check_condition(body, {"path": "response.numFound", "op": "eq", "value": 1})
    assert check_condition(body, {"path": "cluster.live_nodes", "op": "contains", "value": "a"})
    assert not check_condition(body, {"path": "response.numFound", "op": "gte", "value": 5})
    assert not check_condition(body, {"path": "missing.path", "op": "gte", "value": 0})


# ---------------------------------------------------------- lesson engine ---

class ScriptedIO:
    def __init__(self, answers):
        self.answers = list(answers)
        self.log = []

    def say(self, text):
        self.log.append(("say", text))

    def pause(self, prompt=""):
        self.log.append(("pause", prompt))

    def ask(self, q, options):
        self.log.append(("ask", q))
        return self.answers.pop(0)


LESSON = {
    "title": "t",
    "steps": [
        {"say": "hello"},
        {"run": "echo hi"},
        {"http": {"path": "/x", "show": "response.numFound",
                  "expect": {"path": "response.numFound", "op": "eq", "value": 0}}},
        {"wait": "do the thing", "url": "/state",
         "until": {"path": "cluster.live_nodes", "op": "len_eq", "value": 1}},
        {"ask": "q1?", "options": ["a", "b"], "answer": 1, "why": "because"},
        {"ask": "q2?", "options": ["a", "b"], "answer": 0},
    ],
}


def test_run_lesson_scripted():
    states = [{"cluster": {"live_nodes": ["a", "b"]}},   # first poll: not yet
              {"cluster": {"live_nodes": ["a"]}}]        # second: condition met

    def http(method, path, **kw):
        if path == "/x":
            return {"response": {"numFound": 0}}
        return states.pop(0)

    io = ScriptedIO(answers=[1, 1])  # q1 right, q2 wrong
    score = run_lesson(load_lesson(LESSON), "http://base", io=io, http=http,
                       shell=lambda cmd: "hi\n", poll_interval=0, wait_timeout=5)
    assert score == {"asked": 2, "correct": 1}
    kinds = [k for k, _ in io.log]
    assert kinds.count("ask") == 2
    texts = " ".join(t for _, t in io.log)
    assert "$ echo hi" in texts and "condition met" in texts
    assert "correct. because" in texts and "not quite" in texts
    assert not states  # both polls consumed: it actually waited once


def test_wait_timeout_continues():
    io = ScriptedIO(answers=[])
    lesson = {"title": "t", "steps": [
        {"wait": "never happens", "url": "/s",
         "until": {"path": "x", "op": "eq", "value": 1}}]}
    run_lesson(load_lesson(lesson), "http://b", io=io,
               http=lambda m, p, **kw: {"x": 0},
               poll_interval=0, wait_timeout=0.05)
    assert any("timed out" in t for _, t in io.log)


def test_lesson_validation():
    with pytest.raises(SystemExit):
        load_lesson({"title": "t", "steps": [{"bogus": 1}]})
    with pytest.raises(SystemExit):
        load_lesson({"title": "t", "steps": [{"ask": "q"}]})  # no options/answer
    with pytest.raises(SystemExit):
        load_lesson({"title": "t", "steps": [{"wait": "w", "url": "/x"}]})  # no until


def test_builtin_lessons_are_valid():
    lessons = builtin_lessons()
    assert {"cluster-anatomy", "leader-election", "commits-and-visibility"} <= set(lessons)
    for name, lesson in lessons.items():
        load_lesson(lesson)  # must not exit
        kinds = [next(k for k in ("say", "pause", "run", "http", "wait", "ask") if k in s)
                 for s in lesson["steps"]]
        assert "ask" in kinds, name  # every lesson checks understanding
    # the flagship lesson actually waits on real state, twice
    le = lessons["leader-election"]
    waits = [s for s in le["steps"] if "wait" in s]
    assert len(waits) == 2
    assert waits[0]["until"] == {"path": "cluster.live_nodes", "op": "len_eq", "value": 1}


def test_dig_wildcard_fans_out():
    # the analysis API's shape: stage class -> list of tokens
    body = {"index": {"org.a.StandardTokenizer": [{"text": "The"}, {"text": "Runs"}],
                      "org.a.PorterStemFilter": [{"text": "run"}]},
            "segs": {"_0": {"source": "flush"}, "_1": {"source": "merge"}}}
    assert dig(body, "index.*.*.text") == {"org.a.StandardTokenizer": ["The", "Runs"],
                                          "org.a.PorterStemFilter": ["run"]}
    assert dig(body, "segs.*.source") == {"_0": "flush", "_1": "merge"}
    assert dig(body, "segs.*") == body["segs"]
    assert dig({"x": 1}, "x.*") is None


def test_has_value_checks_values_not_keys():
    body = {"segments": {"_0": {"source": "flush"}, "_1": {"source": "merge"}}}
    cond = {"path": "segments.*.source", "op": "has_value", "value": "merge"}
    assert check_condition(body, cond)
    assert not check_condition({"segments": {"_0": {"source": "flush"}}}, cond)
    assert not check_condition({}, cond)
    # contains on the same object checks keys, which is why has_value exists
    assert not check_condition(body, {**cond, "op": "contains"})


def test_render_is_readable():
    # token stages: short class names, aligned, bracketed tokens
    out = render({"org.apache.lucene.analysis.standard.StandardTokenizer": ["The", "Runs"],
                  "org.apache.lucene.analysis.en.PorterStemFilter": ["run"]})
    assert out.splitlines() == ["StandardTokenizer  [The] [Runs]",
                                "PorterStemFilter   [run]"]
    assert render([]) == "(none)" and render({}) == "(none)"
    # a table of objects, narrowed to the columns asked for
    segs = {"_0": {"size": 3, "delCount": 1, "source": "flush", "sizeInBytes": 9}}
    assert render(segs, ["size", "delCount", "source"]) == "_0  size=3  delCount=1  source=flush"
    assert render([{"id": "a", "rating": 5}]) == "id=a  rating=5"
    assert render("msg") == '"msg"' and render(0) == "0"


def test_render_falls_back_to_json_rather_than_hiding_nested_data():
    # a whole response has nested objects; a one-line row would silently drop them
    body = {"responseHeader": {"status": 0, "params": {"q": "x"}}}
    assert '"params"' in render(body)
    assert '"b"' in render([{"a": {"b": 1}}])


def test_engine_mismatch_refuses_to_start():
    lesson = load_lesson({"title": "t", "engine": "solr", "steps": [{"say": "x"}]})
    with pytest.raises(SystemExit, match="solr"):
        run_lesson(lesson, "http://b", io=ScriptedIO([]), engine="opensearch")
    run_lesson(lesson, "http://b", io=ScriptedIO([]), engine="solr")
    with pytest.raises(SystemExit):
        load_lesson({"title": "t", "engine": "mongo", "steps": [{"say": "x"}]})


def test_cleanup_runs_even_when_interrupted():
    calls = []

    def http(method, path, **kw):
        calls.append(path)
        return {"ok": 1}

    class Interrupting(ScriptedIO):
        def pause(self, prompt=""):
            raise KeyboardInterrupt

    lesson = load_lesson({"title": "t", "steps": [{"pause": ""}, {"http": {"path": "/never"}}],
                          "cleanup": [{"http": {"path": "/drop"}}, {"say": "cleaned"}]})
    io = Interrupting([])
    run_lesson(lesson, "http://b", io=io, http=http)
    assert calls == ["/drop"]
    assert ("say", "\ncleaned") in io.log
    with pytest.raises(SystemExit):
        load_lesson({"title": "t", "steps": [{"say": "x"}], "cleanup": [{"ask": "q?"}]})


def test_http_step_shows_request_and_survives_unreachable_cluster():
    import httpx
    io = ScriptedIO([])
    lesson = load_lesson({"title": "t", "steps": [
        {"http": {"path": "/admin/collections",
                  "params": {"action": "LIST", "wt": "json"}, "show": "x"}}]})
    run_lesson(lesson, "http://127.0.0.1:9", io=io)   # nothing listens on port 9
    texts = [t for _, t in io.log]
    assert "\n-> GET /admin/collections?action=LIST" in texts   # wt=json is noise, left out
    assert any(t.startswith("!! ConnectError") for t in texts)
    assert httpx  # the real client was used, not a stub


def test_scratch_collection_lessons_are_self_contained():
    # A lesson that CREATEs a collection must remove it however it ends, and
    # must not name a configset: the Schema API edits the configset, so a
    # lesson on configName=_default rewrote _default for the whole cluster
    # (it happened while writing schema-changes). With none named, Solr makes
    # a private <name>.AUTOCREATED copy and deletes it with the collection.
    for name, lesson in builtin_lessons().items():
        creates = [s["http"]["params"]["name"] for s in lesson["steps"]
                   if "http" in s and (s["http"].get("params") or {}).get("action") == "CREATE"]
        for coll in creates:
            create = next(s["http"]["params"] for s in lesson["steps"]
                          if "http" in s and (s["http"].get("params") or {}).get("name") == coll
                          and s["http"]["params"].get("action") == "CREATE")
            assert "collection.configName" not in create, name
            drops = [s for s in lesson.get("cleanup") or []
                     if "http" in s and s["http"]["params"] == {
                         "action": "DELETE", "name": coll, "wt": "json"}]
            assert drops, f"{name} creates {coll} but never removes it"


def test_new_lessons_ship_and_name_their_engine():
    lessons = builtin_lessons()
    assert {"analysis-chain", "segments-and-merges", "schema-changes"} <= set(lessons)
    for name, lesson in lessons.items():
        assert lesson.get("engine") == "solr", name   # all of them call Solr's API
    orders = sorted(lesson["order"] for lesson in lessons.values())
    assert orders == list(range(1, len(lessons) + 1))   # a course: no gaps, no ties
    merge_wait = next(s for s in lessons["segments-and-merges"]["steps"] if "wait" in s)
    assert merge_wait["until"] == {"path": "segments.*.source", "op": "has_value",
                                   "value": "merge"}


# ----------------------------------------------------------------- explain ---

DEBUG_BODY = {
    "responseHeader": {"QTime": 7},
    "response": {"numFound": 3, "docs": [{"id": "doc-9"}]},
    "debug": {
        "rawquerystring": "title_t:Merging",
        "parsedquery_toString": "title_t:merg",
        "filter_queries": ["category_s:x"],
        "timing": {
            "time": 7.0,
            "prepare": {"time": 1.0, "query": {"time": 1.0}},
            "process": {"time": 6.0, "query": {"time": 2.0},
                        "facet": {"time": 4.0}, "highlight": {"time": 0.0}},
        },
        "explain": {"doc-9": (
            "1.86 = sum of:\n"
            "  1.86 = weight(title_t:merg in 4) [SchemaSimilarity], result of:\n"
            "    1.86 = score(freq=1.0), computed as boost * idf * tf from:\n"
            "      1.20 = idf, computed as log(1 + (N - n + 0.5) / (n + 0.5)) from:\n"
            "        3 = n, number of documents containing term\n"
        )},
    },
}


def test_explain_report_sections():
    out = explain_report(DEBUG_BODY)
    assert "you wrote:   title_t:Merging" in out
    assert "solr ran:    title_t:merg" in out
    assert "analysis chain at work" in out            # stemming detected
    assert "filterCache" in out
    assert "facet 4.0ms" in out
    assert ">> 'facet' dominates" in out              # >50% of total flagged
    assert "why doc 'doc-9' scored" in out
    assert "sum of" in out


def test_timing_and_explain_edge_cases():
    assert "not present" in format_timing({})


def test_debug_component_is_never_the_optimization_target():
    # debug=true is how this report gets its data; on a small index the debug
    # component is most of the time, and must not be named the thing to fix
    timing = {"timing": {"time": 13.0,
                         "prepare": {"time": 0.0, "query": {"time": 0.0}},
                         "process": {"time": 12.0, "query": {"time": 1.0},
                                     "debug": {"time": 11.0}}}}
    out = format_timing(timing)
    assert "'debug' dominates" not in out
    assert "cost of producing this report" in out and "about 2 ms" in out
    # with debug set aside, a component that really dominates is still named
    timing["timing"]["process"]["facet"] = {"time": 1.5}
    assert ">> 'facet' dominates" in format_timing(timing)
    assert "no matching documents" in format_explain({"debug": {}, "response": {"docs": []}})
