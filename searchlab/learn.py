"""Interactive lessons that teach against the live cluster.

A lesson is a YAML script of steps; the engine's distinguishing feature is
the `wait` step: it tells the learner to go do something real — kill a node
in another terminal, index a document — then polls the actual cluster state
until the condition holds. You don't read about leader election; you cause
one and watch the lesson notice.

Step types:
  say:   explanation text
  pause: "press enter to continue"
  run:   a shell command the lesson executes and shows (usually searchlab itself)
  http:  a request against the cluster, response shown; optional expect check
  wait:  instruction + polled condition against a cluster URL (the magic step)
  ask:   multiple-choice question; score tracked, explanation shown either way

Conditions are {path, op, value}: path is dot-notation into the JSON response
(`cluster.live_nodes` etc.), ops are eq/ne/gte/lte/len_eq/len_gte/contains/
has_value. A `*` in a path fans out over a list or an object's values, so
`segments.*.source` is every segment's source, keyed by segment name.

An http step's `show` picks what to print, and `fields` narrows a table of
objects to the columns worth reading: Solr's raw responses are written for
machines, and a lesson that dumps them buries the one value it is about.

A lesson may name its `engine` (solr, elasticsearch, opensearch): its paths
are that engine's API, and against another one every request would fail in
a way that reads like a broken cluster. `cleanup` steps run however the
lesson ends, Ctrl-C included, so a scratch collection never outlives it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import yaml

STEP_TYPES = {"say", "pause", "run", "http", "wait", "ask"}
CLEANUP_TYPES = {"say", "run", "http"}
ENGINES = {"solr", "elasticsearch", "opensearch"}
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "len_eq": lambda a, b: a is not None and len(a) == b,
    "len_gte": lambda a, b: a is not None and len(a) >= b,
    "contains": lambda a, b: a is not None and b in a,
    # `contains` on an object checks its keys; this checks its values, which
    # is what a `*` path returns (e.g. any segment whose source is "merge")
    "has_value": lambda a, b: a is not None and b in (
        a.values() if isinstance(a, dict) else a),
}


def dig(data: Any, path: str) -> Any:
    parts = path.split(".")
    for i, part in enumerate(parts):
        if part == "*":
            rest = ".".join(parts[i + 1:])
            if isinstance(data, dict):
                return {k: dig(v, rest) if rest else v for k, v in data.items()}
            if isinstance(data, list):
                return [dig(v, rest) if rest else v for v in data]
            return None
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        else:
            return None
    return data


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _label(key: str) -> str:
    # analysis stages are keyed by Java class name; the last part is the name
    # people use ("PorterStemFilter", not org.apache.lucene.analysis.en.…)
    return key.rsplit(".", 1)[-1] if key.count(".") >= 2 and " " not in key else key


def _row(obj: dict, fields: list[str] | None) -> str:
    keys = fields or [k for k, v in obj.items() if _scalar(v)]
    return "  ".join(f"{k}={obj.get(k)}" for k in keys)


def render(value: Any, fields: list[str] | None = None) -> str:
    """A response fragment as a person would want to read it.

    Token lists print as [a] [b] (brackets, because a token can contain a
    space and "no tokens" must look different from one empty token), objects
    of lists or rows print one line per key, and anything irregular falls
    back to JSON.
    """
    if _scalar(value):
        return json.dumps(value)
    if isinstance(value, list) and all(_scalar(v) for v in value):
        return " ".join(f"[{v}]" for v in value) if value else "(none)"
    if isinstance(value, list) and all(
            isinstance(v, dict) and (fields or all(_scalar(x) for x in v.values()))
            for v in value):
        return "\n".join(_row(v, fields) for v in value) if value else "(none)"
    if isinstance(value, dict) and value and all(
            _scalar(v) or isinstance(v, (list, dict)) for v in value.values()):
        width = max(len(_label(k)) for k in value)
        lines = []
        for k, v in value.items():
            if isinstance(v, dict):
                # a row only when flat (or narrowed by `fields`); otherwise the
                # nested parts would vanish without a trace, so show JSON
                if not fields and not all(_scalar(x) for x in v.values()):
                    return json.dumps(value, indent=2)[:800]
                shown = _row(v, fields)
            elif isinstance(v, list) and not all(_scalar(x) for x in v):
                return json.dumps(value, indent=2)[:800]
            else:
                shown = render(v)
            lines.append(f"{_label(k):<{width}}  {shown}")
        return "\n".join(lines)
    if isinstance(value, dict) and not value:
        return "(none)"
    return json.dumps(value, indent=2)[:800]


def check_condition(body: dict, cond: dict) -> bool:
    op = cond.get("op", "eq")
    if op not in _OPS:
        sys.exit(f"searchlab: unknown condition op '{op}' — valid: {', '.join(_OPS)}")
    return _OPS[op](dig(body, cond["path"]), cond["value"])


def load_lesson(source: str | Path | dict) -> dict:
    lesson = source if isinstance(source, dict) else yaml.safe_load(Path(source).read_text())
    for key in ("title", "steps"):
        if key not in lesson:
            sys.exit(f"searchlab: lesson needs '{key}'")
    if lesson.get("engine") not in (None, *ENGINES):
        sys.exit(f"searchlab: lesson engine '{lesson['engine']}' — valid: "
                 f"{', '.join(sorted(ENGINES))}")
    for i, step in enumerate(lesson.get("cleanup") or []):
        if not any(k in step for k in CLEANUP_TYPES):
            sys.exit(f"searchlab: cleanup step {i + 1} must be one of "
                     f"{', '.join(sorted(CLEANUP_TYPES))}")
    for i, step in enumerate(lesson["steps"]):
        kind = next((k for k in STEP_TYPES if k in step), None)
        if kind is None:
            sys.exit(f"searchlab: step {i + 1} has no recognized type "
                     f"({', '.join(sorted(STEP_TYPES))})")
        if kind == "ask" and ("options" not in step or "answer" not in step):
            sys.exit(f"searchlab: ask step {i + 1} needs 'options' and 'answer'")
        if kind == "wait" and "until" not in step:
            sys.exit(f"searchlab: wait step {i + 1} needs an 'until' condition")
    return lesson


def builtin_lessons() -> dict[str, dict]:
    out = {}
    for f in resources.files("searchlab").joinpath("lessons").iterdir():
        if f.name.endswith(".yaml"):
            lesson = yaml.safe_load(f.read_text())
            out[f.name[:-5]] = lesson
    return out


def http_request(base_url: str, method: str, path: str, params: dict | None = None,
                 json: Any = None) -> dict:
    url = path if path.startswith("http") else base_url + path
    try:
        r = httpx.request(method, url, timeout=30, params=params, json=json)
    except httpx.HTTPError as e:
        # a traceback here reads as a broken tool; this reads as the
        # cluster being unreachable, which is what it is
        return {"_error": f"{type(e).__name__}: {e}"}
    try:
        return r.json()
    except ValueError:
        return {"_status": r.status_code, "_text": r.text[:500]}


def run_shell(cmd: str) -> str:
    # Lessons run `searchlab …`. The venv's bin sits next to this interpreter
    # and is often not on PATH (searchlab started by full path, or by the
    # dashboard), so put it first rather than fail with "command not found".
    env = dict(os.environ, PATH=os.path.dirname(sys.executable) + os.pathsep
               + os.environ.get("PATH", ""))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, timeout=120)
    return r.stdout + (r.stderr if r.returncode else "")


def request_line(spec: dict) -> str:
    # the request is half the lesson: show what was asked, minus the
    # response-format plumbing every call carries
    params = "&".join(f"{k}={v}" for k, v in (spec.get("params") or {}).items()
                      if k not in ("wt", "json.nl"))
    return f"{spec.get('method', 'GET')} {spec['path']}" + (f"?{params}" if params else "")


def shown(spec: dict, body: dict) -> str:
    """What an http step prints for a response."""
    return render(dig(body, spec["show"]) if spec.get("show") else body, spec.get("fields"))


class IO:
    """Terminal interaction; tests inject a scripted replacement."""

    def say(self, text: str) -> None:
        print(text)

    def pause(self, prompt: str = "\n[enter to continue]") -> None:
        input(prompt)

    def ask(self, question: str, options: list[str]) -> int:
        print(f"\n?  {question}")
        for i, opt in enumerate(options):
            print(f"   {chr(97 + i)}) {opt}")
        while True:
            raw = input("   your answer: ").strip().lower()
            if raw and raw[0] in "abcdefgh"[: len(options)]:
                return ord(raw[0]) - 97
            print(f"   (a-{chr(96 + len(options))})")


def run_lesson(
    lesson: dict,
    base_url: str,
    io: IO | None = None,
    http: Callable | None = None,
    shell: Callable | None = None,
    poll_interval: float = 2.0,
    wait_timeout: float = 300.0,
    engine: str | None = None,
) -> dict:
    """Returns {asked, correct}. base_url is the engine root (spec.base_url());
    engine, when given, is the running cluster's, checked against the lesson's."""
    io = io or IO()
    if engine and lesson.get("engine") and lesson["engine"] != engine:
        sys.exit(f"searchlab: this lesson is written against {lesson['engine']}'s API; "
                 f"the running cluster is {engine}")

    http = http or (lambda method, path, **kw: http_request(base_url, method, path, **kw))
    shell = shell or run_shell

    asked = correct = 0

    def http_step(spec: dict) -> dict:
        io.say(f"\n-> {request_line(spec)}")
        body = http(spec.get("method", "GET"), spec["path"],
                    params=spec.get("params"), json=spec.get("json"))
        if isinstance(body, dict) and "_error" in body:
            io.say(f"!! {body['_error']}")
            return body
        io.say(shown(spec, body))
        return body

    io.say(f"\n=== {lesson['title']} ===")
    if lesson.get("intro"):
        io.say(lesson["intro"])

    try:
        for step in lesson["steps"]:
            if "say" in step:
                io.say("\n" + step["say"])
            elif "pause" in step:
                io.pause()
            elif "run" in step:
                io.say(f"\n$ {step['run']}")
                io.say(shell(step["run"]).rstrip())
            elif "http" in step:
                spec = step["http"]
                body = http_step(spec)
                if "expect" in spec and not check_condition(body, spec["expect"]):
                    io.say(f"!! unexpected state ({spec['expect']['path']}) — "
                           "the lesson may not behave as written from here")
            elif "wait" in step:
                io.say(f"\n>> {step['wait']}")
                cond, path = step["until"], step["url"]
                deadline = time.time() + wait_timeout
                while time.time() < deadline:
                    body = http("GET", path, params=step.get("params"))
                    if check_condition(body, cond):
                        io.say("   ... condition met — the cluster did its thing.")
                        break
                    time.sleep(poll_interval)
                else:
                    io.say("   ... timed out waiting; continuing anyway.")
            elif "ask" in step:
                asked += 1
                idx = io.ask(step["ask"], step["options"])
                if idx == step["answer"]:
                    correct += 1
                    io.say("   correct. " + step.get("why", ""))
                else:
                    right = step["options"][step["answer"]]
                    io.say(f"   not quite — the answer is: {right}. " + step.get("why", ""))
    except KeyboardInterrupt:
        io.say("\n(stopped)")
    finally:
        for step in lesson.get("cleanup") or []:
            if "say" in step:
                io.say("\n" + step["say"])
            elif "run" in step:
                shell(step["run"])
            elif "http" in step:
                http_step(step["http"])

    io.say(f"\n=== done: {correct}/{asked} questions correct ===" if asked
           else "\n=== done ===")
    return {"asked": asked, "correct": correct}


# ------------------------------------------------------------ in a browser ---
# The control panel runs the same lessons one step at a time. It names a
# built-in lesson and a step number, never a request: the server looks the
# step up and runs it, so the panel can't be used to send arbitrary calls.


def lesson_catalog(engine: str | None) -> list[dict]:
    out = []
    for name, les in builtin_lessons().items():
        mismatch = bool(engine and les.get("engine") and les["engine"] != engine)
        out.append({"name": name, "title": les["title"], "requires": les.get("requires", ""),
                    "intro": les.get("intro", ""), "order": les.get("order", 99),
                    "engine": les.get("engine"), "steps": len(les["steps"]),
                    "runnable": not mismatch,
                    "why_not": (f"Written against {les['engine']}'s API; this cluster is "
                                f"{engine}.") if mismatch else ""})
    return sorted(out, key=lambda x: (x["order"], x["name"]))


def _step(name: str, index: int, cleanup: bool = False) -> dict | None:
    lessons = builtin_lessons()
    if name not in lessons:
        raise KeyError(f"no lesson '{name}'")
    lesson = lessons[name]
    steps = (lesson.get("cleanup") or []) if cleanup else lesson["steps"]
    return steps[index] if 0 <= index < len(steps) else None


def web_step(name: str, index: int, base_url: str, cleanup: bool = False,
             http: Callable | None = None, shell: Callable | None = None) -> dict:
    """Run one step and describe it for the page. An ask step is sent without
    its answer; the page asks web_answer once the learner has chosen."""
    http = http or (lambda method, path, **kw: http_request(base_url, method, path, **kw))
    shell = shell or run_shell
    step = _step(name, index, cleanup)
    if step is None:
        return {"kind": "end"}
    if "say" in step:
        return {"kind": "say", "text": step["say"]}
    if "pause" in step:
        return {"kind": "pause"}
    if "run" in step:
        return {"kind": "run", "command": step["run"], "output": shell(step["run"]).rstrip()}
    if "http" in step:
        spec = step["http"]
        body = http(spec.get("method", "GET"), spec["path"],
                    params=spec.get("params"), json=spec.get("json"))
        out = {"kind": "http", "request": request_line(spec)}
        if isinstance(body, dict) and "_error" in body:
            out["error"] = body["_error"]
            return out
        out["output"] = shown(spec, body)
        if "expect" in spec:
            out["unexpected"] = not check_condition(body, spec["expect"])
        return out
    if "wait" in step:
        return {"kind": "wait", "text": step["wait"]}
    return {"kind": "ask", "question": step["ask"], "options": step["options"]}


def web_poll(name: str, index: int, base_url: str, http: Callable | None = None) -> dict:
    http = http or (lambda method, path, **kw: http_request(base_url, method, path, **kw))
    step = _step(name, index)
    if not step or "wait" not in step:
        raise KeyError(f"step {index + 1} of '{name}' is not a wait step")
    body = http("GET", step["url"], params=step.get("params"))
    if isinstance(body, dict) and "_error" in body:
        return {"met": False, "error": body["_error"]}
    return {"met": bool(check_condition(body, step["until"]))}


def web_answer(name: str, index: int, choice: int) -> dict:
    step = _step(name, index)
    if not step or "ask" not in step:
        raise KeyError(f"step {index + 1} of '{name}' is not a question")
    return {"correct": choice == step["answer"], "right": step["options"][step["answer"]],
            "why": step.get("why", "")}
