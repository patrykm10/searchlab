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


def js_object(html, name):
    """Pull a JS object literal out of the template and parse it.

    These literals are JSON apart from the bare `es:` / `solr:` keys and
    the trailing commas JS allows, so quote the one and drop the other.
    """
    start = html.index(f"const {name} = {{")
    body = html[start + len(f"const {name} = "):]
    depth, end = 0, None
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end, f"could not find the end of the {name} literal"
    literal = body[:end]
    literal = re.sub(r"([{,]\s*)(es|solr):", r'\1"\2":', literal)
    literal = re.sub(r",(\s*[}\]])", r"\1", literal)
    return json.loads(literal)


@pytest.fixture(scope="module")
def help_entries(html):
    return js_object(html, "PARAM_HELP")


@pytest.fixture(scope="module")
def doc_links(html):
    return js_object(html, "PARAM_DOCS")


@pytest.fixture(scope="module")
def doc_roots(html):
    return js_object(html, "DOC_ROOT")


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


def test_doc_links_point_at_the_official_documentation(doc_roots):
    """Anything else is someone's blog post, which ages differently from
    the engine."""
    assert doc_roots["es"] == "https://docs.opensearch.org/latest/"
    assert doc_roots["solr"] == "https://solr.apache.org/guide/solr/latest/"
    for root in doc_roots.values():
        assert root.startswith("https://") and root.endswith("/")


def test_every_documented_parameter_has_help(doc_links, help_entries):
    """A link with no explanation next to it is a link nobody hovers."""
    assert not [k for k in doc_links if k not in help_entries]


NOT_AN_ENGINE_PARAM = {"q-load-rps", "ctl-model", "ctl-embed-field"}


def test_every_parameter_links_to_its_own_engine_docs(doc_links, help_entries):
    """The rate control and the embedding controls are not engine
    parameters — fastembed's model list and the field picker have no Solr
    or OpenSearch reference page to send someone to — so they are the
    things with nowhere to link."""
    missing = [f"{key}/{engine}"
               for key, variants in help_entries.items()
               for engine in variants
               if key not in NOT_AN_ENGINE_PARAM and engine not in doc_links.get(key, {})]
    assert not missing, f"no doc link for: {missing}"
    assert not NOT_AN_ENGINE_PARAM & doc_links.keys()


def test_doc_paths_are_relative_to_the_root(doc_links):
    """Paths get concatenated onto DOC_ROOT, so an absolute one would
    quietly produce a broken URL rather than failing loudly."""
    for key, variants in doc_links.items():
        for engine, path in variants.items():
            assert not path.startswith(("http", "/")), f"{key}/{engine}: {path}"
            # Solr's guide is generated HTML pages; OpenSearch's is directories
            if engine == "solr":
                assert path.endswith(".html"), f"{key}: {path}"
            else:
                assert path.endswith("/"), f"{key}: {path}"


def test_clicking_a_label_opens_its_docs_without_breaking_checkboxes(html):
    """Two of these labels wrap their own checkbox — hijacking the click
    there would stop the box toggling."""
    handler = html[html.index('label.addEventListener("click"'):]
    handler = handler[:handler.index("});")]
    assert 'ev.target.tagName === "INPUT"' in handler
    assert "ev.preventDefault()" in handler
    assert 'window.open(url, "_blank", "noopener")' in handler


def test_the_card_survives_the_trip_to_its_own_link(html):
    """The link lives in the card, so leaving the label cannot dismiss it
    immediately or the link is unreachable."""
    assert "scheduleHideParamHelp" in html
    assert 'helpCard.addEventListener("mouseenter"' in html
    assert 'label.addEventListener("mouseleave", scheduleHideParamHelp)' in html


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
