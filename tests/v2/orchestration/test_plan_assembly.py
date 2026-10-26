"""From the solver's resolved inputs to a solved plan: `assemble_and_solve_plan`
(#1303, #1380, #1417, #1577, #1797) (#1837).

Every input reaches its own role: swapping two that share a shape changes
the plan, and swapping a forecast with its own band cannot even build. The
period-0 diagnostic, on at DEBUG, names what blocks the previous plan's
first period and leaves the published plan exactly as it was.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
import solver_plan
import solver_publish as publish
import solver_writer as sw

AEST = timezone(timedelta(hours=10))
N = 8
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=AEST)
# Risk aversion on, so the bands enter the objective and a band swap is real.
CFG = {
    "solver_risk_aversion": 0.4,
    "solver_import_price_risk_aversion": 0.5,
    "solver_export_price_risk_aversion": 0.5,
    "solver_charge_taper_start_percent": 100,
}


class Envelope:
    initial_soc_kwh = 20.0
    min_soc_kwh = 4.0
    max_soc_kwh = 38.0
    charge_discharge_efficiency = 0.95


class LowEnvelope(Envelope):
    initial_soc_kwh = 4.5  # half a kWh above the floor


def _inputs(envelope=Envelope, **overrides):
    """A solvable day in which every same-shaped input holds distinct values."""
    kwargs = {
        "now": NOW,
        "grid_times": [NOW + timedelta(minutes=30 * i) for i in range(N)],
        "period_hours_arr": [0.5] * N,
        "n_periods": N,
        "soc_envelope": envelope(),
        "capacity_kwh": 40.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 25.0,
        "charge_cost": 0.005,
        "discharge_cost_arr": [0.01] * N,
        "salvage_value": 0.06,
        "spike_override_kw": None,
        "import_price": [0.10, 0.60, 0.12, 0.62, 0.14, 0.64, 0.16, 0.66],
        "export_price": [0.03, 0.40, 0.04, 0.42, 0.05, 0.44, 0.06, 0.46],
        "import_price_upper": [0.30, 0.90, 0.32, 0.92, 0.34, 0.94, 0.36, 0.96],
        "export_price_lower": [0.01, 0.10, 0.01, 0.12, 0.01, 0.14, 0.01, 0.16],
        "export_bonus_price": [0.00, 0.20, 0.00, 0.22, 0.00, 0.24, 0.00, 0.26],
        "import_limit_kw": 15.0,
        "export_limit_kw": 30.0,
        "p2p_recent_volume_kwh": 0.0,
        "solar_kw": [0.0, 2.0, 5.0, 8.0, 6.0, 3.0, 1.0, 0.0],
        "solar_lower_kw": [0.0, 1.0, 3.0, 5.0, 4.0, 2.0, 0.5, 0.0],
        "solar_upper_kw": [0.0, 3.0, 7.0, 11.0, 9.0, 5.0, 2.0, 0.0],
        "load_kw": [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5],
        "load_lower_kw": [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5],
        "load_upper_kw": [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0],
        "previous_plan": None,
    }
    kwargs.update(overrides)
    return kwargs


def _assemble(**kwargs):
    return solver_plan.assemble_and_solve_plan(dict(CFG), **kwargs)


def _fingerprint(assembly):
    plan = assembly.plan
    arrays = (
        plan.battery_charge_kw,
        plan.battery_discharge_kw,
        plan.grid_import_kw,
        plan.grid_export_kw,
        plan.battery_soc_kwh,
    )
    return (
        tuple(np.round(np.asarray(a, dtype=float), 6).tobytes() for a in arrays),
        round(float(plan.total_cost), 8),
        plan.status,
    )


@pytest.fixture(scope="module")
def baseline():
    return _assemble(**_inputs())


def test_a_forecast_swapped_with_its_own_band_cannot_build(
    diagnostic_on, caplog, monkeypatch, cadence
):
    # test_a_forecast_swapped_with_its_own_band_cannot_build
    for a, b in [
        ("load_kw", "load_lower_kw"),
        ("load_kw", "load_upper_kw"),
        ("solar_kw", "solar_lower_kw"),
        ("solar_kw", "solar_upper_kw"),
    ]:
        kwargs = _inputs()
        kwargs[a], kwargs[b] = kwargs[b], kwargs[a]
        with pytest.raises(ValueError, match="confidence band"):
            _assemble(**kwargs)

    # test_a_previous_first_period_the_reserve_forbids_is_named_and_changes_nothing: Half a kWh above the floor, cheap import now: the solve charges. The
    caplog.clear()
    with monkeypatch.context() as mp:
        kwargs = _inputs(LowEnvelope)
        free = _assemble(**kwargs)
        net0 = float(free.plan.battery_discharge_kw[0]) - float(
            free.plan.battery_charge_kw[0]
        )
        assert net0 < -0.05
        previous = _with_period0(free.plan, 5.0)
        records = []
        real = solver_plan.period0_crossing_delta

        def spy(*a, **kw):
            records.append(real(*a, **kw))
            return records[-1]

        mp.setattr(solver_plan, "period0_crossing_delta", spy)
        with caplog.at_level(logging.DEBUG, logger=diagnostic_on.name):
            on = _assemble(**dict(kwargs, previous_plan=previous))
        record = records[-1]
        assert (record["pinned_status"], record["delta_objective"]) == (
            "infeasible",
            None,
        )
        assert [b.split(":", 1)[0] for b in record["pinned_blockers"]] == [
            "discharge_reserve"
        ]
        assert record["pinned_participant"] == "home"
        assert "blocked_by(home)=discharge_reserve:" in caplog.text
        diagnostic_on.setLevel(logging.INFO)
        off = _assemble(**dict(kwargs, previous_plan=previous))
        assert _fingerprint(on) == _fingerprint(off)

    # test_the_first_solve_of_each_wall_clock_interval_ranges_and_the_rest_defer
    first = _at(ON)
    assert first.plan.grid_signals is not None and not first.flex_ranging_deferred
    later = _at(ON, NOW + timedelta(seconds=17))
    assert later.plan.grid_signals is None and later.flex_ranging_deferred
    assert not _at(ON, NOW + timedelta(minutes=5)).flex_ranging_deferred
    # UTC 5-minute slots, not five minutes since the last ranging.
    sw.reset_flex_ranging_cadence()
    edge = NOW.replace(minute=4, second=50)
    _at(ON, edge)
    assert not _at(ON, edge + timedelta(seconds=20)).flex_ranging_deferred

    # test_a_deferred_cycle_holds_the_last_real_signals
    with monkeypatch.context() as mp:
        posted = _posts(mp)
        sw.publish_flex_signals(SimpleNamespace(grid_signals=None), hold_last=True)
        assert posted == []  # nothing to hold yet
        sw.publish_flex_signals(_at(ON).plan, hold_last=False)
        (real,) = posted
        assert real[0] == "sensor.nimbus_flex_signals"
        sw.publish_flex_signals(SimpleNamespace(grid_signals=None), hold_last=True)
        assert posted[-1] == real  # the same payload, its own generated_at kept
        sw.publish_flex_signals(SimpleNamespace(grid_signals=None), hold_last=False)
        assert len(posted) == 2  # switched off: nothing posted

    # test_a_deferred_cycle_holds_the_last_telemetry_record
    with monkeypatch.context() as mp:
        publish.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        record = {"interval_start_utc": "2026-09-28T02:00:00Z"}
        kw = {
            "batteries": [],
            "import_price": 0.1,
            "export_price": 0.05,
            "import_limit_kw": 15.0,
            "export_limit_kw": 30.0,
            "period_hours": 0.5,
        }
        posted = _posts(mp)
        mp.setattr(
            sw,
            "build_flex_telemetry_record",
            lambda *a, **k: SimpleNamespace(
                record=record, reason="", clamped_fields=()
            ),
        )
        publish.publish_flex_telemetry_record({}, None, NOW, **kw)
        first = posted[-1]
        mp.setattr(
            sw,
            "build_flex_telemetry_record",
            lambda *a, **k: SimpleNamespace(
                record=None, reason="no ranging", clamped_fields=()
            ),
        )
        publish.publish_flex_telemetry_record({}, None, NOW, hold_last=True, **kw)
        assert posted[-1] == first
        publish.publish_flex_telemetry_record({}, None, NOW, hold_last=False, **kw)
        # #1634: not held -- the reason is published, never the stale record.
        assert posted[-1][1] == "no_record"
        assert posted[-1][2]["last_record_interval"] == record["interval_start_utc"]


# --- the period-0 diagnostic (#1417, #1577) ---------------------------------------------------


@pytest.fixture
def diagnostic_on():
    logger = solver_plan.PERIOD0_PIN_LOGGER
    prior = logger.level
    logger.setLevel(logging.DEBUG)
    yield logger
    logger.setLevel(prior)


def _with_period0(plan, net_kw):
    charge = np.array(plan.battery_charge_kw, dtype=float)
    discharge = np.array(plan.battery_discharge_kw, dtype=float)
    charge[0], discharge[0] = max(-net_kw, 0.0), max(net_kw, 0.0)
    return dataclasses.replace(
        plan, battery_charge_kw=charge, battery_discharge_kw=discharge
    )


# --- flex ranging once per 5-minute interval (#489) ------------------------------------------

ON = {**CFG, "solver_flex_signals_enabled": True}
OFF = {**CFG, "solver_flex_signals_enabled": False}


def _at(cfg, now=NOW):
    return solver_plan.assemble_and_solve_plan(dict(cfg), **_inputs(now=now))


@pytest.fixture
def cadence():
    sw.reset_flex_ranging_cadence()
    yield
    sw.reset_flex_ranging_cadence()


def _posts(monkeypatch):
    posted = []
    monkeypatch.setattr(sw, "ha_post_state", lambda *a, **kw: posted.append(a))
    return posted
