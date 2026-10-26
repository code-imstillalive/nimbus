"""A meter that records only its changes is read as held between them, so a
load model does not learn "always on" (#1556) (#1837).
"""

from __future__ import annotations

import asyncio
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import _ml_path  # noqa: F401
from nimbus_load.ml import model as M

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import homeassistant.util as _ha_util

_ha_util.dt.as_local = lambda x: x
_ha_util.dt.as_utc = lambda x: x
_ha_util.dt.utcnow = lambda: datetime.now(UTC)

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import (
    CONF_HYBRID_RECENT_DAYS,
    CONF_TRAINING_SOURCE,
    STALE_MODEL_WARN_DAYS,
    TRAINING_SOURCE_HYBRID,
    TRAINING_SOURCE_LTS,
    TRAINING_SOURCE_RECORDER,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator

START = datetime(2026, 9, 1, tzinfo=UTC)
END = START + timedelta(days=30)
NAN = float("nan")


def _idle_heater_events() -> list[tuple[datetime, float]]:
    """0 kW for 30 days except one 2-hour run a week: a change-only meter
    writes a row only at each switch."""
    ev = [(START, 0.0)]
    for week in range(4):
        on = START + timedelta(days=7 * week + 3, hours=15)
        ev.append((on, 3.0))
        ev.append((on + timedelta(hours=2), 0.0))
    return ev


def _train(events, change_only_from):
    return M.train_model(
        load_events=events,
        temp_events=[],
        humidity_events=[],
        curtailment_events=[],
        start=START,
        end=END,
        resample_minutes=15,
        min_training_points=500,
        change_only_from=change_only_from,
    )


# --- the model: change-only semantics ---------------------------------------


def test_a_nan_marker_is_never_an_observation():
    grid = [START + timedelta(minutes=15 * i) for i in range(4)]
    ev = [(START, 1.0), (START + timedelta(minutes=10), NAN)]
    assert M.resample_observed_mask(ev, grid) == [True, False, False, False]


def test_an_outage_is_excluded_from_training_rows():
    ev = [(START, 0.5)]
    out_at = START + timedelta(days=10)
    ev.append((out_at, NAN))
    ev.append((out_at + timedelta(days=2), 0.5))
    with_outage = _train(ev, START)
    without = _train([(START, 0.5)], START)
    assert with_outage is not None and without is not None
    # Two days of 15-min points (plus the lag warm-up after the gap) drop out.
    dropped = without.training_points - with_outage.training_points
    assert 2 * 96 <= dropped <= 2 * 96 + M.LAG_LONG_STEPS + 1


# --- the coordinator: markers, downtime, boundary, stale model ---------------


class _FakeState:
    def __init__(self, state, last_changed, unit="kW"):
        self.state = state
        self.last_changed = last_changed
        self.attributes = {"unit_of_measurement": unit}


def _bare(options=None):
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    executor = MagicMock()
    executor.async_add_executor_job = AsyncMock(side_effect=_run_executor)
    coordinator_module.get_instance = MagicMock(return_value=executor)
    coord.entry = MagicMock()
    coord.entry.options = options or {}
    coord.subentry = MagicMock()
    coord.subentry.title = "CB-PW HEATER Power"
    return coord


def _states(rows):
    coordinator_module.get_significant_states = MagicMock(
        return_value={"sensor.x": [_FakeState(s, t) for t, s in rows]}
    )


def test_training_fetch_marks_unavailable_and_unknown_as_gaps():
    coord = _bare()
    t = START
    _states(
        [
            (t, "0.0"),
            (t + timedelta(hours=1), "unavailable"),
            (t + timedelta(hours=2), "0.0"),
            (t + timedelta(hours=3), "unknown"),
        ]
    )
    ev = asyncio.run(coord._async_fetch_training_history("sensor.x", START, END))
    assert ev[0][1] == 0.0
    assert math.isnan(ev[1][1]) and math.isnan(ev[3][1])
    assert len(ev) == 4


def test_ha_downtime_becomes_a_gap_marker():
    coord = _bare()
    down = START + timedelta(days=3)
    coord._retrain_downtime = [(down, down + timedelta(hours=4))]
    _states([(START, "0.0"), (down + timedelta(hours=4), "0.0")])
    ev = asyncio.run(coord._async_fetch_training_history("sensor.x", START, END))
    assert [t for t, _ in ev] == [START, down, down + timedelta(hours=4)]
    assert math.isnan(ev[1][1])


def test_change_only_boundary_follows_the_training_source():
    rec = _bare({CONF_TRAINING_SOURCE: TRAINING_SOURCE_RECORDER})
    lts = _bare({CONF_TRAINING_SOURCE: TRAINING_SOURCE_LTS})
    hyb = _bare(
        {CONF_TRAINING_SOURCE: TRAINING_SOURCE_HYBRID, CONF_HYBRID_RECENT_DAYS: 5}
    )
    assert rec._change_only_from(START, END) == START
    assert lts._change_only_from(START, END) is None
    assert hyb._change_only_from(START, END) == END - timedelta(days=5)


def test_a_failing_retrain_flags_a_stale_model_once(caplog):
    coord = _bare()
    coord._trained = MagicMock()
    coord._trained.trained_at = datetime.now(UTC) - timedelta(
        days=STALE_MODEL_WARN_DAYS + 25
    )
    with caplog.at_level("WARNING"):
        coord._note_retrain_without_model()
        coord._note_retrain_without_model()
    stale = [
        r for r in caplog.records if "still serving the model trained" in r.getMessage()
    ]
    assert len(stale) == 1
    assert "CB-PW HEATER Power" in stale[0].getMessage()
    assert coord._retrain_failing_since is not None


def test_a_recent_model_is_not_flagged(caplog):
    coord = _bare()
    coord._trained = MagicMock()
    coord._trained.trained_at = datetime.now(UTC) - timedelta(days=1)
    with caplog.at_level("WARNING"):
        coord._note_retrain_without_model()
    assert not [r for r in caplog.records if "still serving" in r.getMessage()]
    assert coord._retrain_failing_since is not None


def test_model_age_helper():
    age = coordinator_module._model_age_days(datetime.now(UTC) - timedelta(days=3))
    assert age is not None and 2.99 < age < 3.01
    assert coordinator_module._model_age_days(None) is None
    assert coordinator_module._model_age_days(datetime(2026, 1, 1)) is None  # noqa: DTZ001 -- naive on purpose
