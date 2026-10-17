"""nimbus #1835 (TD944, 17 Oct 2026): a load forecast's lower bound read
-0.215 kW, while the load's own sensor touches 0 and is never negative.

The band is clamped to the signal's observed range plus a 20% margin. For a
load whose history reaches 0, that floor sat 20% of the range below zero, and
the GBRT winner's quantile bounds were not clamped to 0 on their own path, so
the negative bound reached the published forecast.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import coordinator

LOAD_HISTORY = np.array([0.0, 0.2, 0.6, 1.1, 0.0, 0.4, 0.9])  # touches 0


def test_a_loads_floor_is_zero_not_below():
    floor, ceiling = coordinator.band_limits(LOAD_HISTORY, allow_negative=False)
    assert floor == 0.0
    assert ceiling == 1.1 + 0.2 * 1.1


def test_the_reporters_bound_is_lifted_to_zero():
    """TD944's row: value 0.641, lower -0.215. Clamped as the coordinator
    clamps it, the lower bound is 0."""
    floor, _ = coordinator.band_limits(LOAD_HISTORY, allow_negative=False)
    assert max(-0.215, floor) == 0.0


def test_a_never_negative_power_signal_is_floored_at_zero_too():
    """A solar or whole-house Power Signal may go negative by type, but its
    own history never has."""
    floor, _ = coordinator.band_limits(LOAD_HISTORY, allow_negative=True)
    assert floor == 0.0


def test_a_signed_signal_keeps_its_margin_below_its_minimum():
    battery = np.array([-40.0, -10.0, 0.0, 12.0, 30.0])
    floor, ceiling = coordinator.band_limits(battery, allow_negative=True)
    assert floor == -40.0 - 0.2 * 70.0
    assert ceiling == 30.0 + 0.2 * 70.0


def test_a_flat_history_still_gets_a_margin_above():
    floor, ceiling = coordinator.band_limits(np.zeros(5), allow_negative=False)
    assert floor == 0.0
    assert ceiling == 0.2  # max(|0|, 1.0) * 0.2


def test_the_coordinator_clamps_both_band_paths_with_it():
    """The GBRT quantile path and the residual path both pass through the
    same floor: the clamp sits after the if/else."""
    src = inspect.getsource(coordinator.NimbusCoordinator._async_update_data)
    call = src.index("band_limits(")
    assert call < src.index("if has_model_bounds:")
    assert src.index("lower = max(lower, bound_floor)") > src.index("upper = v + band")
