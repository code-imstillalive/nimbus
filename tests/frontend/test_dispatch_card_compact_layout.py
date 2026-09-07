"""Acceptance test for nimbus issue #459 (Mark Purcell): the compact,
phone-width forecast-table layout on `nimbus-dispatch-card-v4.js`.

Approach: parse the CSS embedded in the card's JS file with `tinycss2`
and assert on the rule structure. Zero new infra -- no browser, no Node,
no visual-diff step. This proves:

1. The wide (default) format shows the pill share bar and hides the dot,
   shows the inline lowercase "now" tag and hides the compact "NOW"
   tag -- Raf's v0.94.157/.158 rendering is preserved.
2. A single `@container ftable (max-width: 500px)` rule flips the table
   into the compact format when its wrap resolves narrow.
3. Inside that container rule, the table uses `table-layout: fixed` with
   no `min-width` floor; TIME is exactly 5ch; SOURCE is exactly 1ch;
   the compact spans replace the wide spans on both the source cell and
   the now-row time cell.

What it does not catch: a browser bug that ignores the rule, a visual
regression inside the wide format, or a real-device container-width
edge. Those need a browser harness (Playwright), which nimbus does not
have today -- worth a follow-up if the compact-layout logic ever grows.
"""

from __future__ import annotations

import pathlib
import re
from typing import Iterable

import pytest
import tinycss2

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CARD_JS = (
    REPO_ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-dispatch-card-v4.js"
)


# --------------------------------------------------------------------------- #
# CSS extraction                                                              #
# --------------------------------------------------------------------------- #


def _extract_shadow_css(js_src: str) -> str:
    """Concatenate every single-quoted string literal between the
    `'<style>'` opener and the `'</style>'` closer in the card's
    shadowRoot.innerHTML assignment.

    The card file emits CSS as many `'...' +` string literals in a JS
    concatenation. JS `//` comment lines between literals are skipped.
    Escaped characters common to JS string literals are unescaped so
    tinycss2 sees the real CSS bytes.
    """
    lines = js_src.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if start is None and "'<style>'" in line:
            start = i
        elif start is not None and "'</style>'" in line:
            end = i
            break
    if start is None or end is None:
        raise AssertionError(
            "could not locate <style>...</style> boundaries in the card JS"
        )
    lit = re.compile(r"'((?:[^'\\]|\\.)*)'")
    parts: list[str] = []
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        for m in lit.finditer(line):
            parts.append(m.group(1))
    return (
        "".join(parts)
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\n", "\n")
    )


@pytest.fixture(scope="module")
def css_text() -> str:
    return _extract_shadow_css(CARD_JS.read_text())


@pytest.fixture(scope="module")
def top_rules(css_text: str):
    return tinycss2.parse_stylesheet(
        css_text, skip_comments=True, skip_whitespace=True
    )


# --------------------------------------------------------------------------- #
# Rule helpers                                                                #
# --------------------------------------------------------------------------- #


def _iter_qualified(rules) -> Iterable:
    """Yield qualified (non-at) rules in order."""
    for r in rules:
        if r.type == "qualified-rule":
            yield r


def _prelude_text(rule) -> str:
    return tinycss2.serialize(rule.prelude).strip()


def _decls(rule) -> dict[str, str]:
    """Return a {property: value} dict for a qualified rule's declarations."""
    out: dict[str, str] = {}
    for d in tinycss2.parse_declaration_list(
        rule.content, skip_comments=True, skip_whitespace=True
    ):
        if d.type == "declaration":
            out[d.lower_name] = tinycss2.serialize(d.value).strip()
    return out


def _find_rule(rules, selector_predicate) -> object | None:
    """Return the first qualified rule whose selector matches the predicate."""
    for r in _iter_qualified(rules):
        if selector_predicate(_prelude_text(r)):
            return r
    return None


def _find_container(top_rules, expected_prelude: str):
    """Return the @container at-rule whose prelude matches expected_prelude
    (normalized whitespace). Fails the test with a clear message otherwise.
    """
    for r in top_rules:
        if r.type == "at-rule" and r.at_keyword == "container":
            prelude = _prelude_text(r)
            if " ".join(prelude.split()) == " ".join(expected_prelude.split()):
                return r
    preludes = [
        _prelude_text(r)
        for r in top_rules
        if r.type == "at-rule" and r.at_keyword == "container"
    ]
    raise AssertionError(
        f"@container rule with prelude '{expected_prelude}' not found. "
        f"Saw preludes: {preludes}"
    )


def _container_body_rules(container_rule):
    """tinycss2 stores an at-rule's block body under `.content` as a raw
    component-value list -- parse it as a nested stylesheet."""
    inner_css = tinycss2.serialize(container_rule.content)
    return tinycss2.parse_stylesheet(
        inner_css, skip_comments=True, skip_whitespace=True
    )


# --------------------------------------------------------------------------- #
# Wide-format defaults                                                        #
# --------------------------------------------------------------------------- #


def test_wide_default_pill_visible_and_dot_hidden(top_rules):
    """At the default (wide) container width, the pill share bar shows and
    the split dot is hidden -- Raf's v0.94.157/.158 rendering."""
    wide = _find_rule(top_rules, lambda s: s == ".src-wide")
    assert wide is not None, ".src-wide default rule missing"
    d = _decls(wide)
    assert d.get("display") == "inline-flex", (
        f".src-wide should default to inline-flex, got {d.get('display')!r}"
    )

    compact = _find_rule(top_rules, lambda s: s == ".src-compact")
    assert compact is not None, ".src-compact default rule missing"
    assert _decls(compact).get("display") == "none", (
        ".src-compact should default to display: none on wide cards"
    )


def test_wide_default_now_tag_wide_visible_compact_hidden(top_rules):
    """At the default (wide) container width, the inline lowercase 'now'
    tag renders and the compact 'NOW' badge is hidden."""
    wide = _find_rule(top_rules, lambda s: s == ".time-nowtag-wide")
    assert wide is not None, ".time-nowtag-wide default rule missing"
    assert _decls(wide).get("display") == "inline"

    compact = _find_rule(top_rules, lambda s: s == ".time-nowtag-compact")
    assert compact is not None, ".time-nowtag-compact default rule missing"
    assert _decls(compact).get("display") == "none"


def test_source_dot_class_defined(top_rules):
    """The 9px split dot itself must exist as a class so the source cell's
    compact span has something to render."""
    dot = _find_rule(top_rules, lambda s: s == ".source-dot")
    assert dot is not None, ".source-dot class missing"
    d = _decls(dot)
    assert "border-radius" in d and "50%" in d["border-radius"], (
        ".source-dot must be a circle (border-radius: 50%)"
    )
    # Width must be a small px value -- allow 8-12px inclusive.
    width = d.get("width", "")
    m = re.match(r"(\d+)px", width)
    assert m and 8 <= int(m.group(1)) <= 12, (
        f".source-dot width should be ~9px (a 1ch column fits), got {width!r}"
    )

    seg_a = _find_rule(top_rules, lambda s: s == ".source-dot .seg-a")
    seg_b = _find_rule(top_rules, lambda s: s == ".source-dot .seg-b")
    assert seg_a is not None and seg_b is not None, (
        ".source-dot must define .seg-a and .seg-b sub-rules"
    )
    # Colours from the legend: orange (solar) and blue (grid).
    assert _decls(seg_a).get("background") == "#ffb340"
    assert _decls(seg_b).get("background") == "#4fa3ff"


# --------------------------------------------------------------------------- #
# Compact @container block                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def compact_container(top_rules):
    return _find_container(top_rules, "ftable (max-width: 500px)")


@pytest.fixture(scope="module")
def compact_rules(compact_container):
    return _container_body_rules(compact_container)


def test_compact_container_exists_exactly_once(top_rules):
    matches = [
        r
        for r in top_rules
        if r.type == "at-rule"
        and r.at_keyword == "container"
        and _prelude_text(r).startswith("ftable")
    ]
    assert len(matches) == 1, (
        f"expected exactly one 'ftable' @container rule, found {len(matches)}"
    )


def test_compact_table_layout_is_fixed_with_no_min_width_floor(compact_rules):
    """Inside the compact block, the table drops its min-width floor and
    switches to fixed layout so browsers cannot redistribute leftover
    width as empty padding."""
    r = _find_rule(compact_rules, lambda s: s == "table.ftable")
    assert r is not None, "table.ftable rule missing from compact @container"
    d = _decls(r)
    assert d.get("table-layout") == "fixed", (
        f"compact table.ftable should be table-layout: fixed, "
        f"got {d.get('table-layout')!r}"
    )
    assert d.get("min-width") == "0", (
        f"compact table.ftable should reset min-width to 0, "
        f"got {d.get('min-width')!r}"
    )


def test_compact_time_col_is_exactly_5ch(compact_rules):
    """TIME becomes exactly 5 characters (HH:MM or the NOW badge)."""
    r = _find_rule(
        compact_rules,
        lambda s: "th.time-col" in s and "td.time-col" in s,
    )
    assert r is not None, "compact th/td.time-col rule missing"
    d = _decls(r)
    assert d.get("width") == "5ch", f"time-col width should be 5ch, got {d.get('width')!r}"
    assert d.get("min-width") == "5ch"
    assert d.get("max-width") == "5ch"


def test_compact_source_col_is_exactly_1ch(compact_rules):
    """SOURCE becomes exactly 1 character wide (a single dot)."""
    r = _find_rule(
        compact_rules,
        lambda s: "th.source-col" in s and "td.source-col" in s,
    )
    assert r is not None, "compact th/td.source-col rule missing"
    d = _decls(r)
    assert d.get("width") == "1ch"
    assert d.get("min-width") == "1ch"
    assert d.get("max-width") == "1ch"


def test_compact_flips_wide_and_compact_span_visibility(compact_rules):
    """The container query flips both source-cell spans and both time-cell
    now-tag spans. Together with the wide-default assertions above, this
    is the two-line proof that the container query drives the swap."""
    wide = _find_rule(compact_rules, lambda s: s == ".src-wide")
    compact = _find_rule(compact_rules, lambda s: s == ".src-compact")
    assert wide is not None and compact is not None, (
        "compact block must override both .src-wide and .src-compact"
    )
    assert _decls(wide).get("display") == "none"
    assert _decls(compact).get("display") == "inline-flex"


def test_compact_now_row_shows_now_tag_not_clock(compact_rules):
    """On the now row inside the compact block, the clock text is hidden
    and the compact NOW badge renders (top row is always the current
    period, so the clock value would be redundant)."""
    clock_hidden = _find_rule(
        compact_rules,
        lambda s: "tr.now-row" in s and "time-clock" in s,
    )
    assert clock_hidden is not None, (
        "compact block must hide .time-clock on the now-row"
    )
    assert _decls(clock_hidden).get("display") == "none"

    tag_shown = _find_rule(
        compact_rules,
        lambda s: "tr.now-row" in s and "time-nowtag-compact" in s,
    )
    assert tag_shown is not None, (
        "compact block must show .time-nowtag-compact on the now-row"
    )
    assert _decls(tag_shown).get("display") == "inline"


# --------------------------------------------------------------------------- #
# Row rendering: the HTML the CSS is targeting must actually exist            #
# --------------------------------------------------------------------------- #


def test_row_html_emits_both_source_spans_and_both_time_tags():
    """Static grep on the JS source: every forecast row includes both the
    .src-wide and .src-compact source cell wrappers, and the now-row
    time cell emits both .time-nowtag-wide and .time-nowtag-compact.
    Without this, the CSS rules above would have nothing to target and
    the test would still pass on empty markup."""
    src = CARD_JS.read_text()
    for expected in (
        'class="src-wide"',
        'class="src-compact"',
        'class="time-nowtag-wide',
        'class="time-nowtag-compact',
        'class="time-clock"',
        'class="source-dot',
        'class="num-full"',
        'class="num-round"',
        'class="num col-fees"',
        'class="num col-p2p"',
    ):
        assert expected in src, (
            f"row rendering must include '{expected}' -- the CSS above "
            f"targets it and would be silently no-op otherwise"
        )


# --------------------------------------------------------------------------- #
# Compact column drop + rounding                                              #
# --------------------------------------------------------------------------- #


def test_wide_default_num_full_visible_num_round_hidden(top_rules):
    """Wide-card default: precise .num-full values render, rounded
    .num-round values are hidden."""
    full = _find_rule(top_rules, lambda s: s == ".num-full")
    rnd = _find_rule(top_rules, lambda s: s == ".num-round")
    assert full is not None and rnd is not None, (
        ".num-full and .num-round default rules missing"
    )
    assert _decls(full).get("display") == "inline"
    assert _decls(rnd).get("display") == "none"


def test_compact_hides_fees_column(compact_rules):
    """Compact block hides the fees column entirely (both header and cells).
    Fees are usually 0 in Mark Purcell's data and their header collides
    with adjacent columns at 350px card width."""
    r = _find_rule(
        compact_rules,
        lambda s: "th.col-fees" in s and "td.col-fees" in s,
    )
    assert r is not None, "compact block must hide .col-fees column"
    assert _decls(r).get("display") == "none"


def test_compact_hides_p2p_column(compact_rules):
    """Compact block hides the P2P column entirely."""
    r = _find_rule(
        compact_rules,
        lambda s: "th.col-p2p" in s and "td.col-p2p" in s,
    )
    assert r is not None, "compact block must hide .col-p2p column"
    assert _decls(r).get("display") == "none"


def test_compact_flips_num_full_and_num_round(compact_rules):
    """Inside the compact block, .num-full hides and .num-round shows so
    each numeric cell renders as a whole integer. Values like -0.1 that
    round to 0 are the intended tradeoff for legibility at 350px."""
    full = _find_rule(compact_rules, lambda s: s == ".num-full")
    rnd = _find_rule(compact_rules, lambda s: s == ".num-round")
    assert full is not None and rnd is not None, (
        "compact block must override both .num-full and .num-round"
    )
    assert _decls(full).get("display") == "none"
    assert _decls(rnd).get("display") == "inline"
