"""nimbus #1537 item 1: a price sensor is read in its own unit.

Nimbus read every import/export price sensor as $/kWh. A sensor reporting
c/kWh (LocalVolts v2's Current Buy/Sell Rate state, for one) was therefore
taken 100x too high, silently: 26.5 c/kWh became $26.50/kWh. The forecast
reader, the flat current-value fallback and the period-0 settled-price
override now scale by the sensor's own `unit_of_measurement`. A sensor that
declares no unit, or $/kWh, is read exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import price_intervals
import pytest
import solver_shared
import solver_writer


def _t(s):
    return datetime.fromisoformat(s)


@pytest.mark.parametrize(
    ("unit", "scale"),
    [
        ("c/kWh", 0.01),
        ("¢/kWh", 0.01),
        ("C/KWH", 0.01),
        ("$/kWh", 1.0),
        ("AUD/kWh", 1.0),
        ("$/MWh", 0.001),
        ("c/MWh", 0.00001),
        (None, 1.0),
        ("", 1.0),
        ("kW", 1.0),
    ],
)
def test_unit_scale(unit, scale):
    assert price_intervals.price_unit_scale(unit) == pytest.approx(scale)


def _state(unit, state="26.5", forecast=None):
    attrs = {"forecast": forecast or [], "unit_of_measurement": unit}
    if unit is None:
        del attrs["unit_of_measurement"]
    return {"state": state, "attributes": attrs}


ROWS = [
    {"time": "2026-10-06T13:20:00+10:00", "value": 20.3737},
    {"time": "2026-10-06T13:25:00+10:00", "value": 24.4018},
]
GRID = [_t("2026-10-06T13:20:00+10:00"), _t("2026-10-06T13:25:00+10:00")]


def _read(state):
    with patch.object(solver_writer, "ha_get", return_value=state):
        return solver_writer.resample_generic_price_forecast_with_coverage(
            "sensor.x", GRID
        )


def test_a_cents_forecast_is_read_in_dollars():
    values, real = _read(_state("c/kWh", forecast=ROWS))
    assert values == pytest.approx([0.203737, 0.244018])
    assert real == [True, True]


def test_a_dollar_or_unitless_forecast_is_unchanged():
    dollars = [dict(r, value=r["value"] / 100) for r in ROWS]
    for unit in ("$/kWh", None):
        values, _ = _read(_state(unit, forecast=dollars))
        assert values == [0.203737, 0.244018]


def _both(state):
    """safe_num() reads through solver_shared's own ha_get."""
    return (
        patch.object(solver_writer, "ha_get", return_value=state),
        patch.object(solver_shared, "ha_get", return_value=state),
    )


def test_the_current_price_is_scaled():
    a, b = _both(_state("c/kWh", "26.5"))
    with a, b:
        assert solver_writer.price_now("sensor.x") == pytest.approx(0.265)
    a, b = _both(_state("$/kWh", "0.265"))
    with a, b:
        assert solver_writer.price_now("sensor.x") == pytest.approx(0.265)


def test_an_unreadable_current_price_returns_the_dollar_fallback():
    a, b = _both(_state("c/kWh", "unavailable"))
    with a, b:
        assert solver_writer.price_now("sensor.x", fallback=0.18) == pytest.approx(0.18)


def test_one_read_per_sensor_and_an_unreadable_one_is_no_forecast():
    """The unit comes from the same read as the forecast, so the solve makes
    no more requests than before (the golden master records them). An
    unreadable sensor is no forecast, as it always was."""
    calls = []

    def once(entity_id):
        calls.append(entity_id)
        return _state("c/kWh", forecast=ROWS)

    with patch.object(solver_writer, "ha_get", side_effect=once):
        solver_writer.resample_generic_price_forecast_with_coverage("sensor.x", GRID)
    assert calls == ["sensor.x"]

    def gone(entity_id):
        raise RuntimeError("gone")

    with patch.object(solver_writer, "ha_get", side_effect=gone):
        assert (
            solver_writer.resample_generic_price_forecast_with_coverage(
                "sensor.x", GRID
            )
            is None
        )


# --- nimbus #1623 (IV&V pass): provider-shape sensors never get this scale ---
#
# A provider adapter (AEMO/PD7DAY/Amber/OpenADR, nimbus #1550) already returns
# $/kWh from its own forecast row field, independent of `unit_of_measurement`.
# Scaling by that unit on top -- as every other generic `{time, value}` shape
# correctly does -- would silently corrupt an already-correct price.

_AEMO_ROWS = [
    {
        "start_time": "2026-10-06T13:20:00+10:00",
        "end_time": "2026-10-06T13:25:00+10:00",
        "price": 0.0297,
    },
    {
        "start_time": "2026-10-06T13:25:00+10:00",
        "end_time": "2026-10-06T13:30:00+10:00",
        "price": 0.0301,
    },
]
_AEMO_GRID = [_t("2026-10-06T13:20:00+10:00"), _t("2026-10-06T13:25:00+10:00")]


def _aemo_state(unit):
    attrs = {"forecast": _AEMO_ROWS, "unit_of_measurement": unit}
    if unit is None:
        del attrs["unit_of_measurement"]
    return {"state": "29.7", "attributes": attrs}


@pytest.mark.parametrize("unit", ["$/MWh", "c/kWh", "$/kWh", None])
def test_a_provider_shape_sensor_is_never_rescaled(unit):
    """AEMO's real fixture price (0.0297 $/kWh) must come back unchanged no
    matter what unit_of_measurement the wrapping entity happens to declare --
    that unit describes the entity's own *current-state* basis (AEMO's native
    wholesale price is genuinely $/MWh), not the already-$/kWh forecast rows
    the provider adapter reads directly. Before the #1623 fix, a "$/MWh"
    unit here corrupted 0.0297 into 2.97e-05 -- a 1000x understatement feeding
    straight into the LP."""
    values, real = _read(_aemo_state(unit))
    assert values == pytest.approx([0.0297, 0.0301])
    assert real == [True, True]
