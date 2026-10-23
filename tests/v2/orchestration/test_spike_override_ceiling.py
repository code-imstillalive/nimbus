"""An armed spike rate above the live max-discharge ceiling (#2026).

The live ceiling can sit below the static setting with no configuration
error, so a spike rate within the static setting is clamped to the live
ceiling and warned about once per episode, and the battery accepts it. A rate
above the static setting too is a misconfiguration and is still refused
(#567). Synthetic values only.
"""

from __future__ import annotations

import logging

import pytest
import solver_writer as sw
from solver import elements

LIVE = {"solver_max_discharge_kw": 10.0, "solver_max_discharge_live_entity": "number.x"}


@pytest.fixture(autouse=True)
def _fresh_episode(monkeypatch):
    monkeypatch.setattr(sw, "_SPIKE_OVERRIDE_CLAMP_WARNED", False)


def _battery(spike_kw, max_discharge_kw):
    return elements.BatteryConfig(
        name="b",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=1.0,
        max_soc_kwh=20.0,
        max_charge_kw=10.0,
        max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.1,
        spike_override_discharge_kw=spike_kw,
    )


def test_a_rate_above_only_the_live_ceiling_is_clamped_and_accepted(caplog):
    with caplog.at_level(logging.WARNING):
        kw = sw.clamp_spike_override_to_live_ceiling(LIVE, 10.0, 8.0)
    assert kw == 8.0
    assert "number.x" in caplog.text and "8.00 kW" in caplog.text
    assert _battery(kw, 8.0).spike_override_discharge_kw == 8.0
    with pytest.raises(ValueError):  # what every solve hit before
        _battery(10.0, 8.0)


@pytest.mark.parametrize(
    ("spike_kw", "ceiling", "expected"),
    [(None, 8.0, None), (6.0, 8.0, 6.0), (8.0, 8.0, 8.0), (12.0, 8.0, 12.0)],
    ids=["not_armed", "within", "at_the_ceiling", "above_the_static_setting"],
)
def test_otherwise_the_rate_passes_through_unchanged(
    caplog, spike_kw, ceiling, expected
):
    with caplog.at_level(logging.WARNING):
        assert (
            sw.clamp_spike_override_to_live_ceiling(LIVE, spike_kw, ceiling) == expected
        )
    assert caplog.text == ""


def test_a_rate_above_the_static_setting_is_still_refused():
    static = {"solver_max_discharge_kw": 8.0}
    kw = sw.clamp_spike_override_to_live_ceiling(static, 10.0, 8.0)
    with pytest.raises(ValueError):
        _battery(kw, 8.0)


def test_the_warning_comes_once_per_episode(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            sw.clamp_spike_override_to_live_ceiling(LIVE, 10.0, 8.0)
        sw.clamp_spike_override_to_live_ceiling(LIVE, None, 8.0)  # the spike ends
        sw.clamp_spike_override_to_live_ceiling(LIVE, 10.0, 8.0)  # the next one
    assert (
        sum("live max-discharge ceiling" in r.getMessage() for r in caplog.records) == 2
    )
