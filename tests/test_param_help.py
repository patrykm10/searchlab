"""The query form's hover help.

Every control in the builder is a real engine parameter. The help text is
the part that makes the form teach rather than just submit, so it is worth
guarding: that each entry is actually reachable from a label, that it says
enough to be useful, and that the cross-engine line points at the other
engine rather than the one you are already looking at.
"""

import json
import re
from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).parent.parent / "searchlab" / "templates"
            / "dashboard.html")


@pytest.fixture(scope="module")
def html():
    return TEMPLATE.read_text()


@pytest.fixture(scope="module")
def help_entries(html):
    """Pull PARAM_HELP out of the template and parse it.

    The literal is JSON apart from the bare `es:` / `solr:` keys and the
    trailing commas JS allows, so quote the one and drop the other.
    """
    start = html.index("const PARAM_HELP = {")
    body = html[start + len("const PARAM_HELP = "):]
    depth, end = 0, None
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end, "could not find the end of the PARAM_HELP literal"
    literal = body[:end]
    literal = re.sub(r"^(\s*)(es|solr):", r'\1"\2":', literal, flags=re.M)
    literal = re.sub(r",(\s*[}\]])", r"\1", literal)
    return json.loads(literal)


def test_every_entry_is_reachable_from_a_label(html, help_entries):
    """A help entry nothing points at is dead text."""
    missing = [k for k in help_entries
               if f'label for="{k}"' not in html and f'data-help="{k}"' not in html]
    assert not missing, f"no label or data-help for: {missing}"


def test_every_entry_has_text_for_at_least_one_engine(help_entries):
    empty = [k for k, v in help_entries.items() if not v.get("es") and not v.get("solr")]
    assert not empty


def test_help_says_enough_to_be_worth_hovering(help_entries):
    """Two to four sentences: a one-liner is the tooltip this replaced, and
    a wall of text is not read at all."""
    for key, variants in help_entries.items():
        for engine, entry in variants.items():
            body = entry[0]
            sentences = [s for s in re.split(r"(?<=[.?!])\s+", body.strip()) if s]
            assert 2 <= len(sentences) <= 4, (
                f"{key}/{engine}: {len(sentences)} sentences")
            assert 90 < len(body) < 620, f"{key}/{engine}: {len(body)} chars"


def test_cross_engine_line_points_at_the_other_engine(help_entries):
    """Hovering an OS parameter should name Solr's spelling and the other
    way round — that translation is the reason the lab runs both."""
    for key, variants in help_entries.items():
        for engine, entry in variants.items():
            if len(entry) < 2:
                continue
            cross = entry[1]
            expected = "Solr" if engine == "es" else "OS/ES"
            assert expected in cross, f"{key}/{engine}: {cross!r}"


def test_the_params_worth_translating_are_covered(help_entries):
    """The ones whose names differ between engines are exactly the ones
    someone coming from the other engine goes looking for."""
    for key in ["q-msm", "q-tiebreak", "q-slop", "q-fuzziness", "q-operator",
                "q-qf", "q-rows", "q-fq", "q-from"]:
        assert key in help_entries, key
        variants = help_entries[key]
        assert any(len(e) > 1 for e in variants.values()), (
            f"{key} has no cross-engine line")


def test_solr_only_and_es_only_params_are_not_given_both(help_entries):
    """tie_breaker and slop have no Solr control on this form; offering
    Solr text for them would describe a box that is not there."""
    for key in ["q-mmtype", "q-tiebreak", "q-slop", "q-fuzziness", "q-tth",
                "q-from", "q-minscore", "q-src", "q-conds"]:
        assert "solr" not in help_entries[key], key
    for key in ["q-sort", "q-fl", "q-facet", "q-facet-limit"]:
        assert "es" not in help_entries[key], key


def test_help_is_wired_for_mouse_and_keyboard(html):
    """Hovering the label and tabbing onto the control should do the same
    thing; a hover-only affordance is one the keyboard cannot reach."""
    assert 'label.addEventListener("mouseenter"' in html
    assert 'ctl.addEventListener("focus"' in html
    assert 'if (e.key === "Escape") hideParamHelp()' in html


def test_scroll_repositions_rather_than_dismissing(html):
    """Focusing a control scrolls it into view, so dismissing on scroll
    closes the card the same keystroke just opened."""
    scroll = html[html.index('addEventListener("scroll"'):]
    scroll = scroll[:scroll.index("}, true);")]
    assert "placeParamHelp" in scroll
