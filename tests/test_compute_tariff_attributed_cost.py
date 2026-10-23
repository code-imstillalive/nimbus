"""Real tests of solver_writer.compute_tariff_attributed_cost() -- nimbus
issue #483 (sub-issue 7 of #476), item 2: "the device's share of the
plan's actual grid flow, pro-rata by the device's power in each
period." Item 1 (marginal_cost, network.py's own LP-native shadow-price
figure) already shipped in an earlier pass (v0.94.271) -- this is the
second, deliberately separate figure the same issue asks for.

Pure-Python tests, same convention as test_flow_decomposition.py --
solver_writer.py has no homeassistant.* imports at module scope.
"""

from __future__ import annotations

from types import SimpleNamespace

import _solver_path  # noqa: F401
import numpy as np
import solver_writer

compute_tariff_attributed_cost = solver_writer.compute_tariff_attributed_cost


def _fake_adequacy_load(subentry_id: str | None, power_kw: list[float]):
    return SimpleNamespace(subentry_id=subentry_id, power_kw=np.array(power_kw))


def test_pro_rata_split_across_two_loads_matches_hand_computed_value():
    # Two loads, no PV, no battery -- every watt of house load is
    # grid_to_load, so each device's own share of that flow is exactly
    # its own share of the whole-house load. 1 period, 1 hour, $0.30/kWh.
    load_a = _fake_adequacy_load("a", [3.0])
    load_b = _fake_adequacy_load("b", [1.0])
    result = compute_tariff_attributed_cost(
        [load_a, load_b],
        grid_to_load_kw=np.array([4.0]),
        whole_house_load_kw=np.array([4.0]),
        import_price=np.array([0.30]),
        period_hours=np.array([1.0]),
    )
    # Load A is 3/4 of the house load -> 3/4 of the $1.20 grid cost = $0.90.
    # Load B is 1/4 -> $0.30. Hand-computed, not re-derived from the
    # function's own formula.
    assert abs(result["a"] - 0.9) < 1e-9
    assert abs(result["b"] - 0.3) < 1e-9


def test_zero_contribution_in_a_period_pv_fully_covers_the_load():
    # nimbus issue #483's own Acceptance criterion 1: "marginal costs
    # are approx 0 for periods with PV surplus". grid_to_load=0 this
    # period (PV covered everything) -- the device's own attributed
    # cost for it must be exactly 0, regardless of its own power draw.
    load_a = _fake_adequacy_load("a", [5.0])
    result = compute_tariff_attributed_cost(
        [load_a],
        grid_to_load_kw=np.array([0.0]),
        whole_house_load_kw=np.array([5.0]),
        import_price=np.array([0.30]),
        period_hours=np.array([0.5]),
    )
    assert result["a"] == 0.0


def test_sums_to_the_plans_real_grid_cost_in_a_clean_synthetic_two_load_day():
    # nimbus issue #483's own Acceptance criterion 2, literally: "the
    # tariff-attributed costs sum to the plan's grid cost within
    # rounding" -- exercised in the clean shape the issue's own
    # Acceptance section describes (no export, no battery charging from
    # grid, whole-house load IS exactly the sum of the two configured
    # loads -- no untracked background circuit). Three periods, real
    # varying prices.
    grid_to_load = np.array([2.0, 0.0, 3.5])  # period 1 fully PV-covered
    import_price = np.array([0.25, 0.20, 0.45])
    hours = np.array([0.5, 0.5, 0.5])
    load_a = _fake_adequacy_load("a", [1.5, 0.0, 2.0])
    load_b = _fake_adequacy_load("b", [0.5, 1.2, 1.5])
    whole_house_load = np.array(
        [load_a.power_kw[i] + load_b.power_kw[i] for i in range(3)]
    )
    result = compute_tariff_attributed_cost(
        [load_a, load_b],
        grid_to_load_kw=grid_to_load,
        whole_house_load_kw=whole_house_load,
        import_price=import_price,
        period_hours=hours,
    )
    plan_grid_cost = sum(grid_to_load[i] * import_price[i] * hours[i] for i in range(3))
    assert abs(sum(result.values()) - plan_grid_cost) < 1e-9


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


def test_empty_adequacy_loads_returns_an_empty_dict():
    result = compute_tariff_attributed_cost(
        [],
        grid_to_load_kw=np.array([1.0]),
        whole_house_load_kw=np.array([1.0]),
        import_price=np.array([0.30]),
        period_hours=np.array([1.0]),
    )
    assert result == {}
