"""Household modes (what each one changes, never below zero, the thermal
band kept consistent) and the always-on log buffer behind the health
report (#1837).

Covers what lived in the household-mode, thermal band and health-log files.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

hm = importlib.import_module("custom_components.nimbus_load.household_modes")
health = importlib.import_module("custom_components.nimbus_load.health")

# --- household modes ----------------------------------------------------------------


def test_home_changes_nothing_and_an_unknown_mode_changes_nothing():
    cfg = {"solver_degradation_cost_per_kwh": 0.05}
    for mode in ("home", None, "party"):
        out, applied = hm.apply_to_solver_config(cfg, mode)
        assert out is cfg and applied == {}


@pytest.mark.parametrize(("mode", "factor"), [("away", 1.5), ("economy", 2.0)])
def test_a_mode_scales_the_solvers_degradation_cost(mode, factor):
    cfg = {"solver_degradation_cost_per_kwh": 0.04, "other": 1}
    out, applied = hm.apply_to_solver_config(cfg, mode)
    assert out["solver_degradation_cost_per_kwh"] == pytest.approx(0.04 * factor)
    assert applied == {
        "solver_degradation_cost_per_kwh": out["solver_degradation_cost_per_kwh"]
    }
    assert cfg["solver_degradation_cost_per_kwh"] == 0.04  # the original is untouched


@pytest.mark.parametrize("mode", sorted(hm.LOAD_PRESETS))
def test_each_mode_applies_its_whole_load_table_and_nothing_goes_negative(mode):
    data = {key: 1.0 for key in hm.LOAD_PRESETS[mode]}
    out, applied = hm.apply_to_load_config(data, mode)
    for key, (op, operand) in hm.LOAD_PRESETS[mode].items():
        expected = max(0.0, 1.0 * operand if op == hm.MULTIPLY else 1.0 + operand)
        assert out[key] == pytest.approx(expected)
    assert all(v >= 0.0 for v in out.values())
    assert set(applied) == {k for k in data if out[k] != 1.0}


def test_a_setting_the_load_does_not_have_or_cannot_read_is_left_alone():
    out, applied = hm.apply_to_load_config({"deferrable_target_kwh": "lots"}, "away")
    assert out == {"deferrable_target_kwh": "lots"} and applied == {}
    out, applied = hm.apply_to_load_config({"deferrable_target_kwh": 0.0}, "away")
    assert applied == {}  # 0 stays 0: no change, nothing to report


def test_a_band_with_only_a_floor_is_moved_without_inventing_a_target():
    out, _ = hm.apply_to_load_config({hm.THERMAL_COMFORT_FLOOR_KEY: 40.0}, "away")
    assert out == {hm.THERMAL_COMFORT_FLOOR_KEY: 35.0}


# --- the health log buffer ------------------------------------------------------------


@pytest.fixture
def buffer():
    health.reset_log_buffer_for_tests()
    health.install_log_buffer_handler()
    health.install_log_buffer_handler()  # once per process however often it is asked
    yield logging.getLogger("custom_components.nimbus_load.test")
    health.reset_log_buffer_for_tests()


def test_the_buffer_is_bounded_and_a_broken_message_is_still_kept(buffer):
    for i in range(health._BUFFER_MAXLEN + 5):
        buffer.warning("w%d", i)
    assert len(health.get_recent_log_entries(limit=10_000)) == health._BUFFER_MAXLEN
    assert (
        health.get_recent_log_entries(limit=1)[0]["message"]
        == f"w{health._BUFFER_MAXLEN + 4}"
    )
    # A logging bug in the caller: the record cannot be formatted.
    record = logging.LogRecord(
        "custom_components.nimbus_load.x",
        logging.WARNING,
        __file__,
        1,
        "needs %d args %d",
        (1,),
        None,
    )
    health.NimbusLogBufferHandler().emit(record)
    assert health.get_recent_log_entries(limit=1)[0]["message"] == "needs %d args %d"
