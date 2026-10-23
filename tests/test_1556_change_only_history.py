"""nimbus #1556: load models trained on change-only meters learned "always on".

Home Assistant's recorder writes a row only when a value changes. A circuit
at 0 W for six hours writes nothing for six hours, and is at 0 W throughout.
Under #353's 45-minute staleness cap and #350's "only a new observation is a
target" rule, that idle time produced no training rows at all, so the model
learned only from the moments a circuit was switching or running.

Measured on the reference household's real history (replayed through the
real train_model): a heater with 15 usable points in 30 days and a laundry
circuit with 437, neither able to retrain, while the models still served for
them forecast 3.3 kW and 0.42 kW against 0.00 and 0.10 kW actual.

The fix makes outages explicit (NaN gap markers from unavailable/unknown
states and from Home Assistant's own downtime) so that, for change-only
history, a held value is a real observation. Hourly long-term statistics
keep #350's and #353's rules unchanged.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import homeassistant.util as _ha_util

_ha_util.dt.as_local = lambda x: x
_ha_util.dt.as_utc = lambda x: x
_ha_util.dt.utcnow = lambda: datetime.now(UTC)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


def test_idle_circuit_cannot_train_without_the_fix():
    assert _train(_idle_heater_events(), None) is None


def test_idle_circuit_trains_on_its_idle_time():
    trained = _train(_idle_heater_events(), START)
    assert trained is not None
    # ~30 days of 15-min points, less the lag warm-up.
    assert trained.training_points > 2800


def test_idle_circuit_forecasts_near_zero_not_always_on():
    trained = _train(_idle_heater_events(), START)
    steps = [END + timedelta(minutes=15 * (i + 1)) for i in range(96)]
    res = M.predict(
        trained, steps, [22.0] * 96, [50.0] * 96, [(END - timedelta(hours=1), 0.0)], 15
    )
    mean = sum(res.values) / len(res.values)
    true_mean = 3.0 * 2 / (7 * 24)  # 2 h at 3 kW per week
    assert mean < 0.2, mean
    assert abs(mean - true_mean) < 0.15


def test_a_nan_marker_is_a_gap_until_the_next_reading():
    grid = [START + timedelta(minutes=15 * i) for i in range(12)]
    ev = [
        (START, 1.0),
        (START + timedelta(minutes=40), NAN),
        (START + timedelta(minutes=100), 2.0),
    ]
    vals = M.resample_last_value(ev, grid)
    assert vals[:3] == [1.0, 1.0, 1.0]  # 0, 15, 30 min
    assert vals[3:7] == [None, None, None, None]  # 45..90 min: outage
    assert vals[7:] == [2.0] * 5


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


def test_without_change_only_from_the_original_rules_hold():
    """LTS-shaped hourly buckets: #350 still makes one row per bucket, and
    #353's cap still turns a long silence into None."""
    hourly = [(START + timedelta(hours=h), 0.5 + (h % 3)) for h in range(30 * 24)]
    trained = M.train_model(
        load_events=hourly,
        temp_events=[],
        humidity_events=[],
        curtailment_events=[],
        start=START,
        end=END,
        resample_minutes=15,
        min_training_points=500,
        max_staleness_minutes=75,
    )
    assert trained is not None
    assert trained.training_points <= 30 * 24  # one target per hourly bucket
    grid = [START + timedelta(minutes=15 * i) for i in range(6)]
    capped = M.resample_last_value(
        [(START, 1.0)], grid, max_staleness=timedelta(minutes=45)
    )
    assert capped == [1.0, 1.0, 1.0, 1.0, None, None]


def test_hybrid_applies_each_rule_to_its_own_segment():
    """Hourly buckets before the boundary, change-only rows after it."""
    boundary = END - timedelta(days=5)
    older = [
        (START + timedelta(hours=h), 0.2)
        for h in range(int((boundary - START).total_seconds() // 3600))
    ]
    recent = [(boundary, 0.0)]  # idle for the last 5 days
    trained = M.train_model(
        load_events=older + recent,
        temp_events=[],
        humidity_events=[],
        curtailment_events=[],
        start=START,
        end=END,
        resample_minutes=15,
        min_training_points=500,
        max_staleness_minutes=75,
        change_only_from=boundary,
    )
    assert trained is not None
    older_hours = len(older)
    recent_points = 5 * 96
    # Older segment: at most one row per hourly bucket. Recent segment: every
    # held 15-min point.
    assert trained.training_points <= older_hours + recent_points
    assert trained.training_points >= recent_points


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


def test_forecast_path_fetch_still_drops_non_numeric_states():
    coord = _bare()
    _states([(START, "0.0"), (START + timedelta(hours=1), "unavailable")])
    ev = asyncio.run(coord._async_fetch_recorder_history("sensor.x", START, END))
    assert ev == [(START, 0.0)]


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
