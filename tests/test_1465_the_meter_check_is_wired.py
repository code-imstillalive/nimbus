"""nimbus #1465: three checks that read source text, kept apart until the
decision on source-reading tests (#1837). The reconciliation and the
verdict themselves are in tests/v2/reports/test_quality_score.py."""

from __future__ import annotations

import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def test_the_regret_card_explains_the_meter_code():
    js = (PKG / "frontend" / "nimbus-regret-card.js").read_text(encoding="utf-8")
    assert 'code === "m"' in js
    assert 'attrs.meter_reconciliation.status === "disagrees"' in js


def test_the_scorer_feeds_the_check_into_the_verdict():
    src = (PKG / "solver_reports" / "quality.py").read_text(encoding="utf-8")
    assert 'meter_reconciliation["status"],' in src
    assert '"meter_reconciliation": meter_reconciliation,' in src
    assert '"grid_meter_disagrees"' in src
    sensor = (PKG / "sensor.py").read_text(encoding="utf-8")
    assert (
        "for key in (CONF_SWITCHBOARD_GRID_METER_SENSOR, CONF_GRID_SENSOR):" in sensor
    )
