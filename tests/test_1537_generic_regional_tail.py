"""nimbus #1537 item 4: the generic price branch's tail past a source's own
forecast is the regional wholesale forecast plus the learned retail markup,
as on the price-array branch, instead of the last price held flat.

#1535's capture had 117 of 133 opposing-flow periods inside that flat tail.
Applied only when the regional spot forecast sensor, the regional
current-price sensor and the source's recorded history all exist; otherwise
the tail is held flat exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_writer

T0 = datetime.fromisoformat("2026-10-07T12:00:00+10:00")
GRID = [T0 + timedelta(minutes=30 * i) for i in range(6)]
CFG = {
    "solver_import_price_sensor": "sensor.imp",
    "solver_export_price_sensor": "sensor.exp",
    "solver_regional_spot_forecast_sensor": "sensor.pd7",
    "solver_regional_spot_current_price_sensor": "sensor.spot_now",
}
AEMO = [(T0 + timedelta(minutes=30 * i), 0.10 + 0.01 * i) for i in range(6)]
IMPORT = (
    [0.30, 0.31, 0.32, 0.32, 0.32, 0.32],
    [True, True, True, False, False, False],
    True,
)
EXPORT = (
    [0.08, 0.08, 0.08, 0.08, 0.08, 0.08],
    [True, True, True, True, True, True],
    True,
)


def _patches(offset=None, aemo=AEMO, unit="$/kWh"):
    off = {b: 0.15 for b in range(288)} if offset is None else offset
    return (
        patch.object(solver_writer, "fetch_aemo_forecast", return_value=aemo),
        patch.object(solver_writer, "fetch_price_history", return_value=[(T0, 0.25)]),
        patch.object(solver_writer, "compute_5min_offset", return_value=off),
        patch.object(
            solver_writer,
            "ha_get",
            return_value={"state": "0.3", "attributes": {"unit_of_measurement": unit}},
        ),
    )


def _run(cfg=CFG, imp=IMPORT, exp=EXPORT, **kw):
    a, b, c, d = _patches(**kw)
    with a, b, c, d:
        return solver_writer.extend_generic_price_tails(cfg, GRID, imp, exp)


def test_the_tail_is_wholesale_plus_markup():
    imp, _ = _run()
    assert imp[:3] == [0.30, 0.31, 0.32]  # real coverage untouched
    assert imp[3:] == pytest.approx([0.13 + 0.15, 0.14 + 0.15, 0.15 + 0.15])


def test_a_fully_covered_side_is_untouched():
    _, exp = _run()
    assert exp == EXPORT[0]


def test_without_both_regional_sensors_nothing_changes():
    for missing in (
        "solver_regional_spot_forecast_sensor",
        "solver_regional_spot_current_price_sensor",
    ):
        cfg = {k: v for k, v in CFG.items() if k != missing}
        imp, exp = _run(cfg=cfg)
        assert imp == IMPORT[0] and exp == EXPORT[0]


def test_without_a_learned_markup_or_a_wholesale_forecast_nothing_changes():
    assert _run(offset={})[0] == IMPORT[0]
    assert _run(aemo=[])[0] == IMPORT[0]


def test_a_flat_current_value_fallback_is_not_extended():
    flat = ([0.30] * 6, [True] * 6, False)
    assert _run(imp=flat)[0] == flat[0]


def test_a_missing_bucket_uses_the_mean_markup():
    offset = {0: 0.10, 1: 0.20}  # no bucket for 13:30-14:30
    imp, _ = _run(offset=offset)
    assert imp[3:] == pytest.approx([0.13 + 0.15, 0.14 + 0.15, 0.15 + 0.15])


def test_cents_history_is_scaled_before_learning_the_markup():
    seen = {}

    def offset(history, regional_spot_sensor=None):
        seen["history"] = history
        return {b: 0.15 for b in range(288)}

    a, b, _, d = _patches(unit="c/kWh")
    with (
        a,
        b,
        d,
        patch.object(solver_writer, "compute_5min_offset", side_effect=offset),
    ):
        solver_writer.extend_generic_price_tails(CFG, GRID, IMPORT, EXPORT)
    assert seen["history"] == [(T0, pytest.approx(0.0025))]
