"""Every text a card puts into HTML is escaped, even text Nimbus writes itself.

An injection test (26 Nov 2026) rendered all six cards with real plan data in
a browser, every text value replaced by a script payload: no outside text
(entity names, units, card settings) ran. Two spots still put text into HTML
unescaped: the dispatch card's source labels in its title attribute (today
only Nimbus's own "Solar" / "Grid" / "Load"), and the forecast card's chart
error message (from the ApexCharts card). Both are escaped so a future change
that feeds them other text cannot open a hole.
"""

from __future__ import annotations

import pathlib
import re

FRONTEND = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
)


def _src(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_the_dispatch_card_escapes_its_source_labels():
    src = _src("nimbus-dispatch-card-v4.js")
    m = re.search(r"const srcTitle = hasSource(.*?);\n", src, re.DOTALL)
    assert m, "srcTitle not found"
    body = m.group(1)
    assert "nimbusEscape(p.dispatch_source_a_label" in body
    assert "nimbusEscape(p.dispatch_source_b_label" in body


def test_the_forecast_card_escapes_a_chart_error_message():
    src = _src("nimbus-forecast-card.js")
    m = re.search(
        r"const msg = document\.createElement\(\"ha-card\"\);(.*?)wrap\.appendChild\(msg\)",
        src,
        re.DOTALL,
    )
    assert m, "chart error branch not found"
    body = m.group(1)
    # The message reaches the page as text, never as markup.
    assert "err.message" in body
    assert "innerHTML" not in body or "forecastEscape(" in body
