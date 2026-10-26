"""The stochastic check's sensor (one line a household can read, and its
scenarios on the plan's own grid) and one SoC sensor per battery (#1837).

Covers what lived in #569, #1797 (the stochastic tab) and the stochastic and
per-battery SoC files.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

st = importlib.import_module("custom_components.nimbus_load.sensor_stochastic")
bs = importlib.import_module("custom_components.nimbus_load.sensor_battery_soc")
const = importlib.import_module("custom_components.nimbus_load.const")

ENTRY = SimpleNamespace(entry_id="hub1")
T0 = datetime(2026, 9, 20, 16, 0).astimezone()


def _attach(entity):
    entity.hass = None
    return entity


# --- the headline -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "line"),
    [
        (None, "Not running"),
        ({"status": "optimal"}, "Hedge computed"),
        (
            {
                "status": "optimal",
                "stage1_battery_kw": 2.0,
                "deterministic_period0_battery_kw": 2.04,
            },
            "Agrees with the main plan",
        ),
        (
            {
                "status": "optimal",
                "stage1_battery_kw": 1.5,
                "deterministic_period0_battery_kw": 3.0,
            },
            "Discharges 1.5 kW where the main plan discharges 3.0 kW",
        ),
        (
            {
                "status": "optimal",
                "stage1_battery_kw": 0.0,
                "deterministic_period0_battery_kw": -2.0,
            },
            "Holds where the main plan charges 2.0 kW",
        ),
        (
            {"status": "optimal", "fallback_reason": "no_solar_band"},
            "Main plan: no solar spread",
        ),
        (
            {"status": "skipped", "fallback_reason": "deterministic_too_slow"},
            "Main plan: too slow to check",
        ),
        (
            {"fallback_reason": "cooldown_after:infeasible"},
            "Main plan: waiting to retry",
        ),
        ({"fallback_reason": "error:ValueError"}, "Main plan: check failed"),
        ({"fallback_reason": "something_new"}, "Main plan: something new"),
        ({}, "Main plan: skipped"),
    ],
)
def test_the_headline_says_what_the_check_did_in_plain_words(summary, line):
    assert st.headline(summary) == line


# --- scenarios on the plan's grid ------------------------------------------------------


def _plan(n=4, minutes=30):
    return [
        {
            "time": (T0 + timedelta(minutes=minutes * i)).isoformat(),
            "hours": minutes / 60,
            "soc_pct": 50.0 + i,
        }
        for i in range(n)
    ]


def test_scenarios_on_their_own_grid_are_interpolated_to_each_periods_end():
    ends = [(T0 + timedelta(hours=h)).isoformat() for h in (1, 2)]
    source = {
        "forecast": _plan(4),
        "stochastic_paths": {"ends": ends, "soc_pct": {"p50": [10.0, 20.0]}},
    }
    rows = st.scenario_rows(source)
    # Period ends: 16:30 (before the paths start), 17:00, 17:30 (halfway), 18:00.
    assert [r["p50"] for r in rows] == [None, 10.0, 15.0, 20.0]


@pytest.mark.parametrize(
    "paths",
    [
        {"ends": ["x"], "soc_pct": {"p50": [1]}},
        {"ends": [T0.isoformat()], "soc_pct": {"p50": [1, 2]}},
    ],
)
def test_scenario_times_that_do_not_line_up_fall_back_to_period_by_period(paths):
    rows = st.scenario_rows({"forecast": _plan(2), "stochastic_paths": paths})
    assert [r["p50"] for r in rows] == paths["soc_pct"]["p50"][:2] + [None] * (
        2 - len(paths["soc_pct"]["p50"][:2])
    )


def test_no_plan_or_no_scenarios_is_no_rows_or_only_the_main_plan():
    assert st.scenario_rows({}) == []
    rows = st.scenario_rows({"forecast": [*_plan(1), "junk"], "stochastic_paths": "x"})
    assert rows == [{"time": _plan(1)[0]["time"], "hours": 0.5, "main": 50.0}]
    ends = [T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()]
    gap = st.scenario_rows(
        {
            "forecast": [{"time": T0.isoformat(), "hours": 0.5}],
            "stochastic_paths": {"ends": ends, "soc_pct": {"p": [None, 4.0]}},
        }
    )
    assert gap[0]["p"] is None


# --- one SoC sensor per battery ----------------------------------------------------------


def _participant(sid, name=None):
    return SimpleNamespace(
        subentry_id=sid,
        subentry_type=const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        data={const.CONF_BATTERY_PARTICIPANT_NAME: name} if name else {},
    )


@pytest.mark.parametrize(
    "batteries",
    [
        [],
        [{"name": "Car", "forecast": "x"}],
        [{"name": "Car", "forecast": [{"soc_pct": None}]}],
        ["junk"],
    ],
)
def test_a_battery_the_plan_skipped_keeps_its_last_value(batteries):
    entry = SimpleNamespace(entry_id="hub1", subentries={"a": _participant("a", "Car")})
    _home, car = (_attach(s) for s in bs.create(entry, "1"))
    car.update_from_parent(
        {},
        parent_attributes={
            "batteries": [{"name": "Car", "forecast": [{"soc_pct": 70.0}]}]
        },
    )
    car.update_from_parent(
        {}, parent_attributes={"forecast": _plan(1), "batteries": batteries}
    )
    assert car.native_value == 70.0
