"""nimbus #1465: the regret card explains a day the grid meter disagrees with.
A front-end check (#1837 group C, kept); the meter check itself is in
tests/v2/reports/test_quality_window.py and test_quality_score.py."""

from __future__ import annotations

import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def test_the_regret_card_explains_the_meter_code():
    js = (PKG / "frontend" / "nimbus-regret-card.js").read_text(encoding="utf-8")
    assert 'code === "m"' in js
    assert 'attrs.meter_reconciliation.status === "disagrees"' in js
