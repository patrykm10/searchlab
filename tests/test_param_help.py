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
def metric_entries(html):
    return js_object(html, "METRIC_HELP")


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
                "q-qf", "q-rows", "q-fq", "q-from", "q-highlight",
                # the proximity and boosting knobs: the same three ideas
                # either side, under names that share not one letter
                "q-pf", "q-ps", "q-bf", "q-boostfn", "q-boostfield",
                "q-boostmode"]:
        assert key in help_entries, key
        variants = help_entries[key]
        assert any(len(e) > 1 for e in variants.values()), (
            f"{key} has no cross-engine line")


def test_solr_only_and_es_only_params_are_not_given_both(help_entries):
    """Help for a control the running engine does not show would describe a
    box that is not there. So a parameter one engine genuinely lacks gets
    text for the other engine only — the cross-engine line is where its
    absence gets explained instead."""
    es_only = [
        "q-mmtype",          # Solr picks a parser, not a combining strategy
        "q-fuzziness",       # Solr writes it inline, as term~1
        "q-prefixlen", "q-maxexp", "q-transpose",   # ~ takes no such options
        "q-boostfield", "q-boostmod", "q-boostmode",  # Solr writes functions
        "q-tth", "q-minscore", "q-src", "q-conds",
    ]
    solr_only = [
        # the pf family builds a phrase query out of the query words; ES has
        # no parameter for it, you write the clause yourself
        "q-pf", "q-pf2", "q-pf3", "q-ps", "q-ps2", "q-ps3",
        "q-bq", "q-bf", "q-boostfn",
        "q-sort", "q-fl", "q-facet", "q-facet-limit",
    ]
    for key in es_only:
        assert "solr" not in help_entries[key], key
    for key in solr_only:
        assert "es" not in help_entries[key], key


def test_the_proximity_family_is_all_present(help_entries):
    """Phrase boosting is the half of edismax that slop exists for: ps with
    no pf is slop on a phrase query that was never built. Documenting one
    without the other is what left this form looking like it had no
    proximity controls at all."""
    for key in ["q-pf", "q-ps", "q-pf2", "q-ps2", "q-pf3", "q-ps3"]:
        assert key in help_entries, key
    assert "pf" in help_entries["q-ps"]["solr"][0]


def test_both_engines_can_rank_by_a_field_value(help_entries):
    """bf/boost and function_score are the same feature, and the one thing
    worth carrying across is which of them multiplies and which adds."""
    assert "multiply" in help_entries["q-boostmode"]["es"][0]
    assert "bf" in help_entries["q-boostmode"]["es"][1]
    assert "multiplied" in help_entries["q-boostfn"]["solr"][0]


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


def test_clicking_a_label_opens_its_docs(html):
    handler = html[html.index('label.addEventListener("click"'):]
    handler = handler[:handler.index("});")]
    assert "ev.preventDefault()" in handler
    assert 'window.open(url, "_blank", "noopener")' in handler


def test_a_label_wrapping_its_own_control_keeps_the_click(html):
    """Some labels wrap their own checkbox, where clicking the text is how
    you tick the box. Taking that click for a link leaves the box itself as
    the only target — a few millimetres beside a line of text that navigates
    away instead."""
    assert 'label.querySelector("input, select, textarea")' in html
    assert "if (PARAM_DOCS[key] && !wrapsItsOwnControl)" in html


def test_the_checkbox_labels_this_protects_are_still_there(html):
    """If these stopped wrapping their inputs the guard above would quietly
    stop applying to anything."""
    wrappers = re.findall(r"<label\b[^>]*>.*?</label>", html, re.S)
    wrapping_a_checkbox = [w for w in wrappers if 'type="checkbox"' in w]
    assert len(wrapping_a_checkbox) >= 2
    # and each still names a help entry, or it would lose its hover text too
    assert any("q-explain" in w for w in wrapping_a_checkbox)


def test_hover_help_defaults_off_and_gates_through_one_flag(html):
    """A card that opens over every label passed on the way to a field is in
    the way far more often than it is wanted, so this starts off. The flag
    lives inside showParamHelp — the one function every trigger calls —
    rather than duplicated across hover/focus listeners, or a future trigger
    could add itself without picking up the gate."""
    assert 'let paramHelpOn = false;' in html
    fn = html[html.index("function showParamHelp("):]
    fn = fn[:fn.index("\n}\n")]
    assert "if (!paramHelpOn) return;" in fn.split("\n")[1]


def test_hover_help_toggle_closes_an_open_card_immediately(html):
    """Switching off mid-hover should not wait for whatever mouseleave or
    blur happens to fire next — the card should not outlive the setting
    that was just turned off."""
    block = html[html.index('getElementById("help-toggle")'):]
    block = block[:block.index("})();")]
    assert "hideParamHelp()" in block
    assert 'localStorage.setItem("searchlab-param-help"' in block


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


# ---------- the charts ----------
# A parameter's help says what it changes; a chart's says how to read the
# line. These ride the same card, so they are guarded the same way.

CHARTS = ["m-lt", "m-p99", "m-heap", "m-rate", "m-cpu", "m-gc", "m-seg"]


def test_every_chart_has_help(metric_entries):
    """A chart you cannot interpret is decoration. Two of these had no
    tooltip at all before, which is what prompted the section."""
    assert sorted(metric_entries) == sorted(CHARTS)


def test_every_chart_help_is_reachable_from_its_title(html, metric_entries):
    for key in metric_entries:
        assert f'data-help="{key}"' in html, key


def test_chart_help_reads_the_line_rather_than_defining_the_metric(metric_entries):
    """Same length budget as the parameters: enough to say what a rising
    line means, short enough to actually be read on hover."""
    for key, entry in metric_entries.items():
        bodies = ([entry] if isinstance(entry, list)
                  else list(entry.values()))
        for body in bodies:
            text = body[0]
            sentences = [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s]
            assert 2 <= len(sentences) <= 4, f"{key}: {len(sentences)} sentences"
            assert 90 < len(text) < 620, f"{key}: {len(text)} chars"


def test_charts_split_by_engine_only_where_the_source_differs(metric_entries):
    """Heap is heap. Splitting a metric that both engines measure the same
    way just duplicates the prose and invites the two copies to drift."""
    for key in ["m-p99", "m-rate"]:
        assert isinstance(metric_entries[key], dict), key
        assert set(metric_entries[key]) == {"es", "solr"}, key
    for key in ["m-lt", "m-heap", "m-cpu", "m-gc", "m-seg"]:
        assert isinstance(metric_entries[key], list), key


def test_engine_split_charts_name_the_other_engine(metric_entries):
    for key in ["m-p99", "m-rate"]:
        for engine, entry in metric_entries[key].items():
            expected = "Solr" if engine == "es" else "OS/ES"
            assert expected in entry[1], f"{key}/{engine}: {entry[1]!r}"


def test_charts_are_not_given_dead_documentation_links(doc_links, metric_entries):
    """These are readings, not parameters — there is no reference page for
    "the shape of your heap chart", and the arrow would promise one."""
    assert not set(metric_entries) & set(doc_links)


def test_chart_titles_no_longer_carry_native_tooltips(html):
    """The browser tooltip and the card would otherwise both fire on the
    same hover, saying different things at different speeds."""
    charts = html[html.index('<div class="chartgrid">'):]
    charts = charts[:charts.index("</div>\n  <table id=\"summary\"")]
    # data-help-title is the card's own heading, not a browser tooltip
    assert not re.search(r'(?<![-\w])title=', charts)


def test_chart_titles_are_reachable_by_keyboard(html):
    """A parameter's help opens by tabbing onto the control it describes.
    A chart has no control, so the title has to become the tab stop itself
    or these are the only help on the page a keyboard cannot open."""
    wiring = html[html.index("function wireParamHelp()"):]
    wiring = wiring[:wiring.index("\n  }")]
    assert "document.getElementById(key) || label" in wiring
    assert "label.tabIndex = 0" in wiring


def test_a_click_does_not_flash_the_card(html):
    """Clicking a control focuses it too, and opening on that focus pops
    the card for the instant before the next click dismisses it."""
    assert "focusFromKeyboard" in html
    assert 'addEventListener("mousedown", () => { focusFromKeyboard = false; }' in html
    assert "if (focusFromKeyboard) showParamHelp" in html


def test_the_card_serves_both_maps(html):
    """One card, two sources — a chart key that only PARAM_HELP is consulted
    for would silently show nothing."""
    lookup = html[html.index("function paramHelp(key)"):]
    lookup = lookup[:lookup.index("}")]
    assert "METRIC_HELP" in lookup
    assert "Array.isArray" in lookup, "engine-agnostic entries need a path"
    wiring = html[html.index("function wireParamHelp()"):]
    wiring = wiring[:wiring.index("\n  }")]
    assert "METRIC_HELP" in wiring
