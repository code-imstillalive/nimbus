"""A sensor reporting "nan" or "inf" takes its reader's fallback, never reaches
the plan (#1837).
"""

from __future__ import annotations

import logging
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

    # test_an_entity_that_no_longer_exists_takes_the_fallback (#2182)
    def gone(entity_id):
        raise solver_shared._native_http_error(entity_id, 404, "not found")

    with (
        patch.object(solver_shared, "ha_get", gone),
        patch.object(solver_writer, "ha_get", gone),
    ):
        assert solver_shared.safe_num("number.gone", 80.0) == pytest.approx(80.0)
        assert solver_writer.price_now("sensor.gone", fallback=0.3) == pytest.approx(
            0.3
        )

    # test_a_quiet_read_of_an_unavailable_price_does_not_warn
    state = {"state": "unavailable", "attributes": {"unit_of_measurement": "$/MWh"}}
    for quiet, level in ((False, logging.WARNING), (True, logging.DEBUG)):
        with (
            _states({"sensor.spot": state}),
            patch.object(solver_shared._LOGGER, "log") as log,
            patch.object(solver_shared._LOGGER, "warning") as warning,
            patch.object(solver_shared._LOGGER, "debug") as debug,
        ):
            value = solver_writer.price_now(
                "sensor.spot", fallback=math.nan, quiet=quiet
            )
        assert math.isnan(value)
        assert warning.called is (level == logging.WARNING)
        assert debug.called is (level == logging.DEBUG)
        log.assert_not_called()

    # test_the_aemo_cross_check_skips_a_non_finite_spot_price
    for text in NON_FINITE:
        with _states({"sensor.spot": {"state": text}}):
            result = solver_writer.check_aemo_p5min_disagreement(
                "sensor.spot", 0.25, 0, {0: 0.1}, 0.05
            )
        assert result is None
