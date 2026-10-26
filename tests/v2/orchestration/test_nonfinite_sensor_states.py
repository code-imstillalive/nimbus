"""A sensor reporting "nan" or "inf" takes its reader's fallback, never reaches
the plan (#1837).
"""

from __future__ import annotations

import math
import sys
from contextlib import contextmanager
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


def test_price_now_falls_back_on_a_non_finite_state():
    # test_price_now_falls_back_on_a_non_finite_state
    for text in NON_FINITE:
        state = {"state": text, "attributes": {"unit_of_measurement": "$/kWh"}}
        with _states({"sensor.price": state}):
            value = solver_writer.price_now("sensor.price", fallback=0.3)
        assert value == pytest.approx(0.3)
        assert math.isfinite(value)

    # test_the_aemo_cross_check_skips_a_non_finite_spot_price
    for text in NON_FINITE:
        with _states({"sensor.spot": {"state": text}}):
            result = solver_writer.check_aemo_p5min_disagreement(
                "sensor.spot", 0.25, 0, {0: 0.1}, 0.05
            )
        assert result is None
