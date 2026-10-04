"""nimbus #1531 (tester Chris C): the Topology card's Grid box was cut off
at the top of the diagram on every install that uses the documented card
config.

## The mechanism

The Grid box is placed ABOVE the switchboard bus in diagram coordinates:

    rowTop = 50
    busTop = rowTop - 15        -> 35
    gmH    = 68
    gmY    = busTop - 70        -> -35  (box centre)

so the box spans y = -69 .. -1. The SVG viewBox starts at y = 0, and the
only thing that moved the diagram down into view was the whole-house
header's height, `whHeight` -- 78 when the card YAML sets a `whole_house`
block and 0 otherwise. `docs/dashboards.md`'s documented config has no
such block, so `whHeight` is 0 and the box is drawn entirely above the
visible area. The reference household's dashboard sets `whole_house`, so
it was never seen there.

The fix shifts the diagram by whatever the topmost element needs,
independent of the header:

    diagramTop = gmY - gmH / 2
    topShift   = Math.max(whHeight, Math.ceil(-diagramTop) + 10)

## How this was verified, and what this file can and cannot check

Verified by headless render (Playwright, Chromium) of the real card with a
mock `hass`, measuring the Grid box's bounding rect relative to the SVG:

    original card, no whole_house:   box top at -79 px  (fully clipped)
    original card, whole_house set:  box top at +10 px
    fixed card,    no whole_house:   box top at +11 px
    fixed card,    whole_house set:  box top at +11 px  (1 px from before)

CI has no Node (same posture as every test in this directory), so what is
pinned here is the arithmetic and the structure: the constants are read
out of the real source, the box's top is computed from them, and the
shift expression is required to (a) exist, (b) be what the translate, the
viewBox height and the footer all use, and (c) clear the computed box
top with margin when `whHeight` is 0. If someone moves the Grid box
higher, or puts `whHeight` back in the translate, this fails.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CARD_JS = (
    REPO_ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-topology-card.js"
)


def _source() -> str:
    return CARD_JS.read_text(encoding="utf-8")


def _const(src: str, pattern: str) -> float:
    m = re.search(pattern, src)
    assert m, f"layout constant not found: {pattern!r}"
    return float(m.group(1))


def _grid_box_top(src: str) -> float:
    """The Grid box's top edge in diagram coordinates, from the real
    constants in the source (not transcribed)."""
    row_top = _const(src, r"const rowTop = (\d+(?:\.\d+)?);")
    bus_top_off = _const(src, r"const busTop = rowTop - (\d+(?:\.\d+)?);")
    gm_h = _const(src, r"const gmW = \d+, gmH = (\d+(?:\.\d+)?);")
    gm_y_off = _const(src, r"const gmY = busTop - (\d+(?:\.\d+)?);")
    bus_top = row_top - bus_top_off
    gm_y = bus_top - gm_y_off
    return gm_y - gm_h / 2


def test_grid_box_really_is_above_the_origin():
    # The premise of the bug: without a shift the box is at negative y.
    # If this ever stops being true the shift logic is moot and this file
    # should be rethought, not silently passed.
    assert _grid_box_top(_source()) < 0


def test_top_shift_is_derived_from_the_grid_box_not_the_header():
    src = _source()
    assert "const diagramTop = gmY - gmH / 2;" in src
    m = re.search(
        r"const topShift = Math\.max\(whHeight, Math\.ceil\(-diagramTop\) \+ (\d+)\);",
        src,
    )
    assert m, "topShift must be max(whHeight, ceil(-diagramTop) + margin)"
    margin = int(m.group(1))
    assert margin >= 8, "a margin under 8 px lets the glow filter touch the edge"
    # The arithmetic, evaluated with whHeight = 0 (the documented config):
    box_top = _grid_box_top(src)
    shift_without_header = -box_top + margin
    assert box_top + shift_without_header >= 8


def test_translate_viewbox_and_footer_all_use_top_shift():
    src = _source()
    assert "transform: `translate(0, ${topShift})`" in src
    assert "`0 0 ${totalW} ${totalH + topShift + footerHeight}`" in src
    assert "y: topShift + totalH + 14" in src
    # And the old header-only shift is gone from all three sites.
    assert "translate(0, ${whHeight})" not in src
    assert "${totalH + whHeight + footerHeight}" not in src
    assert "y: whHeight + totalH + 14" not in src


def test_header_is_not_shifted():
    # The whole-house header sits at the top-left and shares no horizontal
    # space with the Grid column; it must stay outside the shifted group,
    # or the fix would push it down and open a blank band at the top.
    src = _source()
    i_shift = src.index("const shiftGroup = svgEl")
    i_wh = src.index("if (whGroup) this._svg.appendChild(whGroup);")
    assert i_wh > i_shift
    assert "shiftGroup.appendChild(whGroup)" not in src
