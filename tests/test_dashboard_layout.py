"""Layout invariants for the query builder.

These are static assertions on the template rather than a rendering test,
because the bug they guard against was static: a <select> was placed in a
fixed 110px grid track sized for a number input, and a grid item will not
shrink a select below its longest option — so it kept its intrinsic width
and drew straight over the control next to it.
"""

import re
from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).parent.parent / "searchlab" / "templates"
            / "dashboard.html")


@pytest.fixture(scope="module")
def html():
    return TEMPLATE.read_text()


def test_ids_are_unique(html):
    """Two elements sharing an id is invalid, and only the first is
    reachable from script — which is how a topology control panel once went
    silently dead."""
    ids = re.findall(r'\bid="([^"]+)"', html)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate ids: {sorted(dupes)}"


def test_parameter_rows_have_no_fixed_narrow_track(html):
    """The rows that hold multi_match type / operator / fuzziness size
    themselves to whatever is visible. A fixed template also breaks when a
    control is hidden: choose query_string and the operator select lands in
    the column that was meant for something else."""
    rule = re.search(r"\.qsplit, \.qsplit3 \{[^}]+\}", html)
    assert rule, "the .qsplit/.qsplit3 rule went missing"
    assert "auto-fill" in rule.group(0)
    assert not re.search(r"grid-template-columns:\s*\d+px", rule.group(0))


def test_grid_and_flex_children_may_shrink(html):
    """min-width: 0 is the whole fix — without it a select refuses to go
    below its longest option and overlaps its neighbour instead."""
    assert re.search(r"\.qsplit > \*, \.qsplit3 > \* \{[^}]*min-width:\s*0",
                     html)
    assert re.search(r"\.cond > \* \{[^}]*min-width:\s*0", html)


def test_clause_rows_are_not_built_with_inline_grid_templates(html):
    """Condition, sort and aggregation rows carry a variable number of
    controls — an aggregation has two or five depending on the type — so
    they wrap instead of being pinned to hand-computed pixel tracks."""
    assert "grid-template-columns:minmax" not in html
    assert 'class="cond" style="grid-template-columns' not in html


def test_dsl_preview_sits_beside_the_builder(html):
    """The preview is the point of the builder; under a screenful of form it
    cannot be watched while the controls move."""
    layout = html.index('<div class="qlayout">')
    form = html.index('<div class="qform">', layout)
    side = html.index('<aside class="qside', form)
    assert form < side
    assert html.index('id="q-dsl-wrap"') > side
    assert re.search(r"\.qside \{[^}]*position:\s*sticky", html) or \
        re.search(r"@media[^{]+\{\s*\.qside \{[^}]*position:\s*sticky", html)


def test_form_is_grouped_into_named_blocks(html):
    """Fourteen rows of equally weighted controls is a list, not a form."""
    groups = re.findall(r'<h3 class="qglabel">(?:<b[^>]*>)?(\w+)', html)
    assert groups == ["Match", "Filter", "Results", "Aggregations"]


def test_aggregation_group_is_not_labelled_twice(html):
    """The block heading names it; the row inside should not repeat it."""
    agg_row = html[html.index('id="q-agg-row"'):]
    agg_row = agg_row[:agg_row.index('id="q-aggs"')]
    assert "<label>Aggregations</label>" not in agg_row


NEW_CONTROLS = ["q-pf", "q-pf2", "q-pf3", "q-ps", "q-ps2", "q-ps3",
                "q-bq", "q-bf", "q-boostfn",
                "q-prefixlen", "q-maxexp", "q-transpose",
                "q-boostfield", "q-boostmod", "q-boostmode"]


def test_every_new_control_exists_and_feeds_the_preview(html):
    """A control the preview never hears about is one whose effect you have
    to run a query to see — which is the thing the side panel exists to
    avoid. The listener list is what wires them, so it is what to check."""
    listeners = html[html.index('for (const id of ["q-q", "q-qf"'):]
    listeners = listeners[:listeners.index("]) {")]
    for cid in NEW_CONTROLS:
        assert f'id="{cid}"' in html, cid
        assert f'"{cid}"' in listeners, f"{cid} does not nudge the preview"


def test_the_parser_decides_which_params_are_on_screen(html):
    """Solr reads a different set of parameters per parser and ignores the
    rest silently, so the form has to say which ones the chosen parser will
    act on — the same job esApplies() already does for ES."""
    assert "function solrApplies()" in html
    body = html[html.index("function solrApplies()"):]
    body = body[:body.index("\nfunction onParserChange")]
    # pf2/pf3 and the multiplicative boost are edismax's alone
    assert "edismaxOnly" in body and 'p === "edismax"' in body


def test_a_hidden_param_is_absent_from_the_request(html):
    """Sending a parameter whose control is off screen is how a query starts
    behaving in a way the form cannot explain."""
    build = html[html.index("function queryBody()"):]
    build = build[:build.index("\n/* ---------- live DSL preview")]
    solr = build[build.index("if (!isES()) {"):build.index("  const a = esApplies();")]
    for guard in ["s.msm", "s.tieBreaker", "s.phraseFields", "s.boosts",
                  "s.edismaxOnly"]:
        assert guard in solr, guard


def test_solr_gets_a_request_preview_too(html):
    """It was already built server-side and never asked for — the panel was
    marked ES-only, so Solr had the one thing the builder is for switched
    off."""
    assert 'class="qside esonly"' not in html
    assert "function solrParamText(" in html
    assert 'out.kind === "params"' in html


def test_highlight_fragments_are_rendered(html):
    """Highlighting was requested and the answer thrown away, which reads as
    a setting that does nothing."""
    assert "out.highlights" in html
    # the marks are the point, so that one tag survives escaping
    assert '.replaceAll("&lt;em&gt;", "<mark>")' in html
    assert re.search(r"\.qhl mark \{", html)


def test_no_top_level_identifier_is_declared_twice(html):
    """The whole page is one script, so a duplicated const is a SyntaxError
    that kills every listener on the page at parse time — the form still
    renders, and nothing on it works. Nothing else here would catch it: the
    markup is fine and the handlers simply never run."""
    script = html[html.index('<script>\n"use strict";'):]
    names = re.findall(r"^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)",
                       script, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"declared more than once at the top level: {dupes}"
