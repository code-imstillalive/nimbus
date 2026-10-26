"""A sensor reporting "nan" or "inf" takes its reader's fallback, never reaches
the plan (#1837).
"""

from __future__ import annotations

import math
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import solver_shared
import solver_writer

NON_FINITE = ["nan", "NaN", "inf", "-inf", "Infinity"]


@contextmanager
def _states(states):
    def ha_get(entity_id):
        return states[entity_id]

    with (
        patch.object(solver_shared, "ha_get", ha_get),
        patch.object(solver_writer, "ha_get", ha_get),
    ):
        yield


def test_finite_float_refuses_only_the_non_finite():
    assert solver_shared.finite_float("2.5") == 2.5
    assert solver_shared.finite_float(-0.0) == 0.0
    for text in NON_FINITE:
        with pytest.raises(ValueError, match="non-finite"):
            solver_shared.finite_float(text)
    with pytest.raises(ValueError):
        solver_shared.finite_float("unknown")


@pytest.mark.parametrize("text", NON_FINITE)
def test_safe_num_falls_back_on_a_non_finite_state(text):
    """Prices' scalar fallback and the battery's live SoC read through it."""
    with _states({"sensor.soc": {"state": text}}):
        assert solver_shared.safe_num("sensor.soc", fallback=42.0) == 42.0


@pytest.mark.parametrize("text", NON_FINITE)
def test_price_now_falls_back_on_a_non_finite_state(text):
    state = {"state": text, "attributes": {"unit_of_measurement": "$/kWh"}}
    with _states({"sensor.price": state}):
        value = solver_writer.price_now("sensor.price", fallback=0.3)
    assert value == pytest.approx(0.3)
    assert math.isfinite(value)


@pytest.mark.parametrize("text", NON_FINITE)
def test_the_aemo_cross_check_skips_a_non_finite_spot_price(text):
    with _states({"sensor.spot": {"state": text}}):
        result = solver_writer.check_aemo_p5min_disagreement(
            "sensor.spot", 0.25, 0, {0: 0.1}, 0.05
        )
    assert result is None


@pytest.mark.parametrize("text", NON_FINITE)
def test_an_envelope_limit_reporting_non_finite_uses_the_static_limit(text):
    grid = [
        datetime(2026, 10, 22, tzinfo=UTC) + timedelta(minutes=30 * i) for i in range(3)
    ]
    with (
        _states({"number.envelope": {"state": text, "attributes": {}}}),
        patch.object(solver_writer, "entity_exists", lambda e: True),
        patch.object(solver_writer, "_kw_scale_factor", lambda e: 1.0),
    ):
        limits = solver_writer.resolve_envelope_limit_kw("number.envelope", 15.0, grid)
    assert limits == [15.0, 15.0, 15.0]
