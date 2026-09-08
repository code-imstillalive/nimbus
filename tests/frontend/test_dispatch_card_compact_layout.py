"""Acceptance test for nimbus issue #459 (Mark Purcell): the compact,
phone-width forecast-table layout on `nimbus-dispatch-card-v4.js`.

Approach: parse the CSS embedded in the card's JS file with `tinycss2`
and assert on the rule structure. Zero new infra -- no browser, no Node,
no visual-diff step. This proves:

1. The wide (default) format shows the pill share bar, hides the split
   dot, shows the inline lowercase "now" tag and hides the compact
   "NOW" badge -- Raf's v0.94.157/.158 rendering is preserved.
2. Header spans .hdr-name and .hdr-unit render inline in wide mode
   (unit sits next to name), and stack as blocks in compact mode.
3. A single `@container ftable (max-width: 500px)` rule flips the table
   into the compact format when its wrap resolves narrow.
4. Inside that container rule: the table switches to auto layout at
   100% width; TIME is exactly 5ch; source, fees, p2p and net columns
   are hidden; buy is red, sell is green; grid-pos is green and
   grid-neg is red (trader convention).
5. Row rendering emits the classes the compact CSS targets, so a
   silent regression to unclassed cells cannot pass the CSS-only test.

What it does not catch: a browser bug that ignores the container rule,
a visual regression inside the wide format, or a real-device
container-width edge. Those need a browser harness, which nimbus does
not have today -- worth a follow-up if the compact-layout logic ever
grows further.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterable

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
    return "".join(parts).replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")


@pytest.fixture(scope="module")
def css_text() -> str:
    return _extract_shadow_css(CARD_JS.read_text())


@pytest.fixture(scope="module")
def top_rules(css_text: str):
    return tinycss2.parse_stylesheet(css_text, skip_comments=True, skip_whitespace=True)


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


def test_wide_default_header_spans_inline(top_rules):
    """Wide-card default: .hdr-name and .hdr-unit sit inline so column
    headers read as one token (e.g. 'BUY \u00a2', 'LOAD kW'). Compact
    stacks them below."""
    r = _find_rule(top_rules, lambda s: s == ".hdr-name, .hdr-unit")
    assert r is not None, "default rule for .hdr-name, .hdr-unit missing"
    assert _decls(r).get("display") == "inline"


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


def test_compact_table_layout_auto_and_full_width(compact_rules):
    """Inside the compact block, the table switches to auto layout at
    100% width so the browser sizes columns to their content."""
    r = _find_rule(compact_rules, lambda s: s == "table.ftable")
    assert r is not None, "table.ftable rule missing from compact @container"
    d = _decls(r)
    assert d.get("table-layout") == "auto", (
        f"compact table.ftable should be table-layout: auto, "
        f"got {d.get('table-layout')!r}"
    )
    assert d.get("min-width") == "0", (
        f"compact table.ftable should reset min-width to 0, got {d.get('min-width')!r}"
    )
    assert d.get("width") == "100%", (
        f"compact table.ftable should span the full container, "
        f"got width {d.get('width')!r}"
    )


def test_compact_time_col_is_exactly_5ch(compact_rules):
    """TIME becomes exactly 5 characters (HH:MM or the NOW badge)."""
    r = _find_rule(
        compact_rules,
        lambda s: "th.time-col" in s and "td.time-col" in s,
    )
    assert r is not None, "compact th/td.time-col rule missing"
    d = _decls(r)
    assert d.get("width") == "5ch", (
        f"time-col width should be 5ch, got {d.get('width')!r}"
    )
    assert d.get("min-width") == "5ch"
    assert d.get("max-width") == "5ch"


def test_compact_header_spans_stack_as_blocks(compact_rules):
    """Inside the compact block, the two header spans render as blocks so
    'Buy' and '\u00a2' stack vertically (matching the 2026-09-07 sample)."""
    name = _find_rule(compact_rules, lambda s: s == "table.ftable thead th .hdr-name")
    unit = _find_rule(compact_rules, lambda s: s == "table.ftable thead th .hdr-unit")
    assert name is not None, "compact block must set .hdr-name to block"
    assert unit is not None, "compact block must set .hdr-unit to block"
    assert _decls(name).get("display") == "block"
    assert _decls(unit).get("display") == "block"


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
# Compact column drops                                                        #
# --------------------------------------------------------------------------- #


def _assert_column_hidden(compact_rules, col_class: str) -> None:
    r = _find_rule(
        compact_rules,
        lambda s: f"th.{col_class}" in s and f"td.{col_class}" in s,
    )
    assert r is not None, f"compact block must hide .{col_class} column"
    assert _decls(r).get("display") == "none"


def test_compact_hides_source_column(compact_rules):
    """Compact block hides the SOURCE column (share pill / dot) entirely."""
    _assert_column_hidden(compact_rules, "col-source")


def test_compact_hides_fees_column(compact_rules):
    """Compact block hides the FEES column entirely."""
    _assert_column_hidden(compact_rules, "col-fees")


def test_compact_hides_p2p_column(compact_rules):
    """Compact block hides the P2P column entirely."""
    _assert_column_hidden(compact_rules, "col-p2p")


def test_compact_hides_net_column(compact_rules):
    """Compact block hides the NET$ column entirely."""
    _assert_column_hidden(compact_rules, "col-net")


# --------------------------------------------------------------------------- #
# Compact colouring: buy red, sell green, grid trader-convention              #
# --------------------------------------------------------------------------- #


def test_compact_buy_color_is_red(compact_rules):
    """Buy data cell and header are red in compact (matches sample)."""
    for selector in ("table.ftable td.buy-color", "table.ftable th.buy-color"):
        r = _find_rule(compact_rules, lambda s, sel=selector: s == sel)
        assert r is not None, f"compact block missing rule for {selector}"
        assert _decls(r).get("color") == "#e04a3f", (
            f"{selector} should be red (#e04a3f), got {_decls(r).get('color')!r}"
        )


def test_compact_sell_color_is_green(compact_rules):
    """Sell data cell and header are green in compact (matches sample)."""
    for selector in ("table.ftable td.sell-color", "table.ftable th.sell-color"):
        r = _find_rule(compact_rules, lambda s, sel=selector: s == sel)
        assert r is not None, f"compact block missing rule for {selector}"
        assert _decls(r).get("color") == "#3ddc84", (
            f"{selector} should be green (#3ddc84), got {_decls(r).get('color')!r}"
        )


def test_compact_grid_pos_green_neg_red(compact_rules):
    """Trader-convention grid colour in compact: positive (import) green,
    negative (export) red. Overrides the wide-format solver-perspective
    inline colour via !important."""
    pos = _find_rule(
        compact_rules,
        lambda s: s == "table.ftable td.col-grid.grid-pos",
    )
    neg = _find_rule(
        compact_rules,
        lambda s: s == "table.ftable td.col-grid.grid-neg",
    )
    assert pos is not None, "compact block missing td.col-grid.grid-pos rule"
    assert neg is not None, "compact block missing td.col-grid.grid-neg rule"
    # Values include '!important' which tinycss2 preserves in the serialised
    # value; assert the hex prefix is right rather than exact equality.
    pos_color = _decls(pos).get("color", "")
    neg_color = _decls(neg).get("color", "")
    assert pos_color.startswith("#3ddc84"), (
        f"grid-pos should be green (#3ddc84...), got {pos_color!r}"
    )
    assert neg_color.startswith("#e04a3f"), (
        f"grid-neg should be red (#e04a3f...), got {neg_color!r}"
    )


# --------------------------------------------------------------------------- #
# Row rendering: the HTML the CSS is targeting must actually exist            #
# --------------------------------------------------------------------------- #


def test_row_html_emits_source_time_and_column_classes():
    """Static grep on the JS source: every forecast row includes the wide
    and compact source spans, the wide and compact now-tag spans, and
    the column-classifier classes the compact CSS targets. Without
    this, the CSS rules above would have nothing to target and the
    test would pass on empty markup."""
    src = CARD_JS.read_text()
    for expected in (
        'class="src-wide"',
        'class="src-compact"',
        'class="time-nowtag-wide',
        'class="time-nowtag-compact',
        'class="time-clock"',
        'class="source-dot',
        # 2026-09-07 (v0.94.163): column classifiers for hide + colour
        "col-source",
        "col-buy buy-color",
        "col-fees",
        "col-sell sell-color",
        "col-p2p",
        "col-load",
        "col-pv",
        "col-batt",
        "col-grid",
        "col-soc",
        "col-net",
        # Header two-line spans
        'class="hdr-name"',
        'class="hdr-unit"',
    ):
        assert expected in src, (
            f"row/header rendering must include '{expected}' -- the CSS "
            f"above targets it and would be silently no-op otherwise"
        )


def test_row_html_emits_grid_sign_class():
    """Static grep: the sign-conditional grid class (grid-pos / grid-neg)
    is emitted so the trader-convention compact colour rule can attach."""
    src = CARD_JS.read_text()
    assert "grid-pos" in src and "grid-neg" in src, (
        "row rendering must emit grid-pos / grid-neg sign classes"
    )
    assert "gridSignClass" in src, (
        "row rendering must derive gridSignClass from the row's grid value"
    )
