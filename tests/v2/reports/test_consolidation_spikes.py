"""The consolidated price keeps a spike that any source forecasts (#1786)
(#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver_inputs import price_consolidation as pc

BASE = 0.30
THRESHOLD = 0.85


def test_a_spike_only_one_source_carries_is_kept():
    series = {
        "lv": [BASE, 15.0, BASE],
        "pd": [BASE, BASE, BASE],
        "amb": [BASE, BASE, BASE],
    }
    median_only, _, _ = pc.consolidate(series)
    kept, _, _ = pc.consolidate(series, spike_threshold=THRESHOLD)
    assert median_only[1] == BASE  # the drop the dry run measured
    assert kept[1] == 15.0


def test_the_highest_source_wins_when_several_see_a_spike():
    series = {"lv": [12.0], "pd": [15.0], "amb": [2.0], "p5": [14.0]}
    values, _, _ = pc.consolidate(series, spike_threshold=THRESHOLD)
    assert values == [15.0]


def test_calm_periods_are_unchanged():
    series = {"lv": [0.31, 0.29], "pd": [0.30, 0.28], "amb": [0.25, 0.40]}
    assert (
        pc.consolidate(series, spike_threshold=THRESHOLD)[0]
        == pc.consolidate(series)[0]
    )


def test_it_also_overrides_learned_weights_in_a_spike_period():
    series = {"lv": [15.0], "pd": [BASE], "amb": [BASE]}
    weights = {"lv": 0.05, "pd": 0.5, "amb": 0.45}
    assert pc.consolidate(series, weights)[0][0] < 2.0  # $15 shrunk to ~$1
    assert pc.consolidate(series, weights, spike_threshold=THRESHOLD)[0] == [15.0]
