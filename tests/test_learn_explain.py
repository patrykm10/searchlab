"""Learn engine and explain tests: scripted lesson runs, condition math,
built-in lesson validation, debug-output translation."""

from __future__ import annotations

import pytest

from searchlab.explain import explain_report, format_explain, format_timing
from searchlab.learn import builtin_lessons, check_condition, dig, load_lesson, run_lesson

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
    assert "no matching documents" in format_explain({"debug": {}, "response": {"docs": []}})
