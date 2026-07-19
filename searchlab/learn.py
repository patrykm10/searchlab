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
(`cluster.live_nodes` etc.), ops are eq/ne/gte/lte/len_eq/len_gte/contains.
"""

from __future__ import annotations

import json
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
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "len_eq": lambda a, b: a is not None and len(a) == b,
    "len_gte": lambda a, b: a is not None and len(a) >= b,
    "contains": lambda a, b: a is not None and b in a,
}


def dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        else:
            return None
    return data


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
) -> dict:
    """Returns {asked, correct}. base_url is the engine root (spec.base_url())."""
    io = io or IO()

    def _http(method: str, path: str, **kw) -> dict:
        url = path if path.startswith("http") else base_url + path
        r = httpx.request(method, url, timeout=15, **kw)
        try:
            return r.json()
        except ValueError:
            return {"_status": r.status_code, "_text": r.text[:500]}

    http = http or _http
    shell = shell or (lambda cmd: subprocess.run(
        cmd, shell=True, capture_output=True, text=True).stdout)

    asked = correct = 0
    io.say(f"\n=== {lesson['title']} ===")
    if lesson.get("intro"):
        io.say(lesson["intro"])

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
            io.say(f"\n-> {spec.get('method', 'GET')} {spec['path']}")
            body = http(spec.get("method", "GET"), spec["path"],
                        params=spec.get("params"), json=spec.get("json"))
            shown = dig(body, spec["show"]) if spec.get("show") else body
            io.say(json.dumps(shown, indent=2)[:800])
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

    io.say(f"\n=== done: {correct}/{asked} questions correct ===" if asked
           else "\n=== done ===")
    return {"asked": asked, "correct": correct}
