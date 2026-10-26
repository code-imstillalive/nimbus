"""A device's share of the plan's real grid cost, pro rata by its power in each
period (#476, #483) (#1837).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import solver_writer

compute_tariff_attributed_cost = solver_writer.compute_tariff_attributed_cost


def _fake_adequacy_load(subentry_id: str | None, power_kw: list[float]):
    return SimpleNamespace(subentry_id=subentry_id, power_kw=np.array(power_kw))


def test_zero_whole_house_load_period_contributes_nothing_not_a_crash():
    # Real edge case, not synthetic paranoia: a period where the whole
    # house genuinely draws nothing (every configured load is off) must
    # not divide by zero -- 0.0 contribution, not a crash.
    load_a = _fake_adequacy_load("a", [0.0])
    result = compute_tariff_attributed_cost(
        [load_a],
        grid_to_load_kw=np.array([0.0]),
        whole_house_load_kw=np.array([0.0]),
        import_price=np.array([0.30]),
        period_hours=np.array([1.0]),
    )
    assert result["a"] == 0.0


def test_load_with_no_subentry_id_is_skipped():
    # Same "not every AdequacyLoadPlan is subentry-backed" guard
    # marginal_cost/profit_horizon's own downstream reader (apply_
    # commanded_state_guard()) already has to handle.
    load_no_id = _fake_adequacy_load(None, [2.0])
    load_real = _fake_adequacy_load("real", [2.0])
    result = compute_tariff_attributed_cost(
        [load_no_id, load_real],
        grid_to_load_kw=np.array([4.0]),
        whole_house_load_kw=np.array([4.0]),
        import_price=np.array([0.30]),
        period_hours=np.array([1.0]),
    )
    assert list(result.keys()) == ["real"]
