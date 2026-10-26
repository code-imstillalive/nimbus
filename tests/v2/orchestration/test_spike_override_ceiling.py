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
