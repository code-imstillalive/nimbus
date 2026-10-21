"""Layer 1 of the rebuilt suite (#1837): contracts the solver area's cutover
check found only an old file holding (tests/v2/solver/CUTOVER.md).

- A matrix entry at or below HiGHS's `small_matrix_value` (1e-9) is dropped
  before the row goes in, so it can neither raise (highspy 1.15 raises on
  HiGHS's drop warning, HAEO #501/#511) nor change the answer; a row whose
  every entry is dropped keeps its bounds, so 0 == 1 stays infeasible
  (#1837, 20 Oct).
- A plan that did not solve hands each consumer its own zero arrays: none
  is the same object as another, so writing into one cannot change the
  rest (#356).
- A committed P2P block (#1761) is an exact ceiling: export inside it never
  exceeds the committed rate, and a block the battery can fund is delivered
  exactly. (Its shortfall price: `test_plan_features`.)
- The failing-model dump (#773) marks a phase dumped only once the write
  succeeds, so a transient failure never blocks the next capture (#1074,
  found in the IV&V pass since #1058).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import CalibratedOptions, LexOptions, LPProblem
from solver.network import _infeasible_plan, build_plan
from test_lp_properties import _build, problems
from test_plan_invariants import SETTINGS, _num

TINY = st.sampled_from([1e-9, -1e-9, 5e-10, -1e-10, 1e-11, 1e-12])


@SETTINGS
@given(problems(), st.data())
def test_a_tiny_row_entry_is_dropped_and_changes_nothing(spec, data):
    """The same problem with a tiny entry added to one row, for a variable
    the row does not hold, solves to the same objective in every mode."""
    plain = _build(spec)
    i = data.draw(st.integers(0, len(spec["rows"]) - 1))
    tiny = data.draw(TINY)
    p = LPProblem()
    for j in range(spec["n"]):
        p.add_variable(f"x{j}", lb=0.0, ub=spec["ub"][j], cost=spec["cost"][j])
        if spec["second"][j] is not None:
            p.set_secondary_cost(f"x{j}", spec["second"][j])
    p.add_variable("extra", lb=0.0, ub=1.0, cost=0.0)
    for k, (a, b, _named) in enumerate(spec["rows"]):
        terms = {f"x{j}": a[j] for j in range(spec["n"]) if a[j] != 0.0} or {"x0": 1.0}
        if k == i:
            terms["extra"] = tiny
        p.add_ub_constraint(terms, b)
    for options in (None, LexOptions(), CalibratedOptions()):
        want = plain.solve(options=options)
        got = p.solve(options=options)
        assert want.status == got.status == "optimal", options
        assert abs(plain.primary_cost_of(want) - p.primary_cost_of(got)) <= 1e-6 * max(
            1.0, abs(plain.primary_cost_of(want))
        ), options


@given(TINY)
def test_a_row_whose_every_entry_is_dropped_keeps_its_bounds(tiny):
    """tiny * x == 1 becomes 0 == 1 once the entry is dropped: infeasible, as
    HiGHS would build it, never silently satisfied."""
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=1.0)
    p.add_eq_constraint({"x": tiny}, 1.0)
    assert p.solve().status != "optimal"


@given(st.integers(min_value=1, max_value=48))
def test_a_plan_that_did_not_solve_shares_no_array(n):
    periods = PeriodGrid(hours=np.ones(n), start=datetime(2026, 1, 1, tzinfo=UTC))
    plan = _infeasible_plan(periods, "infeasible", iterations=0)
    arrays = {
        name: value
        for name, value in vars(plan).items()
        if isinstance(value, np.ndarray)
    }
    assert len(arrays) >= 8  # the check is not vacuous
    ids = [id(a) for a in arrays.values()]
    assert len(ids) == len(set(ids)), "two plan fields are the same array"
    first = next(iter(arrays))
    arrays[first][...] = 5.0
    for name, a in arrays.items():
        if name != first:
            assert not np.any(a == 5.0), name


@SETTINGS
@given(problems(), st.data())
def test_a_tiny_cost_beside_real_ones_still_takes_the_phased_solve(spec, data):
    """The lexicographic bound rows are built from the cost vectors, so a
    float-noise cost lands in a row too: the phased solve still runs (no
    fallback to the plain solve) and holds the primary."""
    j = data.draw(st.integers(0, spec["n"] - 1))
    spec = {**spec, "cost": [*spec["cost"]]}
    spec["cost"][j] = data.draw(TINY)
    p = _build(spec)
    plain = p.solve()
    for options in (LexOptions(), CalibratedOptions()):
        result = p.solve(options=options)
        assert result.status == "optimal", options
        if isinstance(options, CalibratedOptions):
            assert result.calibration_weight_used is not None
        assert p.primary_cost_of(result) <= plain.objective + 1e-6 * max(
            1.0, abs(plain.objective)
        )


@SETTINGS
@given(
    st.integers(min_value=1, max_value=6),
    _num(1.0, 15.0),
    _num(0.05, 1.0),
    _num(0.0, 120.0),
)
def test_a_committed_block_is_an_exact_ceiling(length, rate, export_price, soc):
    """Inside the block export never exceeds the rate, however well export
    pays; with energy enough for the whole block, it is exactly the rate."""
    n = length + 2
    fixed = np.full(n, np.nan)
    fixed[1 : 1 + length] = rate
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.2),
            export_price=np.full(n, export_price),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
            fixed_export_kw=fixed,
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=200.0,
                initial_soc_kwh=max(soc, 2.4),
                min_soc_kwh=2.4,
                max_soc_kwh=200.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.93,
                discharge_efficiency=0.93,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
    )
    assert plan.status == "optimal"
    block = plan.grid_export_kw[1 : 1 + length]
    assert np.all(block <= rate + 1e-6)
    # Fundable from the battery alone, before any charging: deliver it all.
    if (max(soc, 2.4) - 2.4) * 0.93 >= rate * length + 1e-6:
        np.testing.assert_allclose(block, rate, atol=1e-6)


class _Highs:
    def __init__(self, raises=None):
        self.written: list[str] = []
        self._raises = raises

    def writeModel(self, path):
        if self._raises is not None:
            raise self._raises
        self.written.append(path)


@given(st.sampled_from(["phase2_pin_resolve", "phase1_primary", "x y/z"]))
def test_a_failed_dump_never_blocks_the_next_one(label):
    lp._LP_DUMPED_PHASES.clear()
    try:
        assert lp._dump_failing_model(_Highs(OSError("read-only")), label) is None
        working = _Highs()
        path = lp._dump_failing_model(working, label)
        assert path is not None and working.written == [path]
        # Once written, the same phase is not written again.
        again = _Highs()
        assert lp._dump_failing_model(again, label) is None
        assert again.written == []
    finally:
        lp._LP_DUMPED_PHASES.clear()
