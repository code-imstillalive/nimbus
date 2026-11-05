"""A card's background and its text come from the same theme (5 Nov 2026).

A tester on Home Assistant's default light theme saw the Power Flow card's
figures, "Now" and the next step's time as dark text on a dark card. The card
took its background from `--ha-card-background`, falling back to its own dark
colour, while its text took the theme's `--primary-text-color`. The default
theme sets no `--ha-card-background`, so the card fell back to dark while the
text stayed the light theme's dark. The reference household's theme sets
`--ha-card-background`, which is why it never showed there.

A card that offers its own background fallback must fall back through the
theme's `--card-background-color` first, so the background and text agree.
The Power Flow card's House ring and its lines to the circuits, drawn in a
near-white, follow the theme's text colour so they show on a light card.
"""

from __future__ import annotations

import pathlib
import re

FE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
)


def test_a_cards_own_background_falls_back_through_the_theme():
    offenders = []
    for js in sorted(FE.glob("*.js")):
        src = js.read_text(encoding="utf-8")
        for m in re.finditer(r"var\(--ha-card-background,\s*([^;]+?)\)\s*;", src):
            if not m.group(1).startswith("var(--card-background-color"):
                offenders.append((js.name, m.group(0)))
    assert not offenders, offenders

    flow = (FE / "nimbus-flow-card.js").read_text(encoding="utf-8")
    assert ".ring.house { stroke: var(--primary-text-color" in flow
    assert ".flow.house { stroke: var(--primary-text-color" in flow
