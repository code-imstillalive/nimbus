"""A MIP that stops on its time limit keeps the incumbent when the problem opts
in and the gap is small enough (#1805) (#1837).
"""

from __future__ import annotations

import numpy as np
from solver.lp import BlendedOptions, LPProblem
from solver.stochastic_dispatch import (
    JOINT_ACCEPT_MIP_GAP,
)


def _hard_knapsack(accept: float | None) -> LPProblem:
    rng = np.random.default_rng(0)
    p = LPProblem()
    n, m = 60, 40
    for i in range(n):
        p.add_variable(f"x{i}", binary=True, cost=-float(rng.integers(1, 100)))
        p.set_secondary_cost(f"x{i}", 0.001)
    for j in range(m):
        a = rng.integers(1, 100, n).astype(float)
        p.add_ub_constraint(
            {f"x{i}": float(a[i]) for i in range(n)}, float(a.sum() / 2)
        )
    p.time_limit_seconds = 0.3
    p.quiet_diagnostics = True
    p.accept_time_limited_mip_gap = accept
    return p


def test_default_keeps_a_timed_out_mip_an_error() -> None:
    result = _hard_knapsack(None).solve(options=BlendedOptions(blend_weight=1.0))
    assert result.status == "error"
    assert result.raw_status == "Time limit reached"


def test_opted_in_keeps_the_incumbent_within_the_gap() -> None:
    p = _hard_knapsack(1.0)
    result = p.solve(options=BlendedOptions(blend_weight=1.0))
    assert result.status == "optimal"
    xs = [p.value_of(result, f"x{i}") for i in range(60)]
    assert all(abs(x - round(x)) < 1e-6 for x in xs)
    assert sum(xs) > 0


def test_opted_in_still_refuses_a_wide_gap() -> None:
    result = _hard_knapsack(1e-12).solve(options=BlendedOptions(blend_weight=1.0))
    assert result.status == "error"


def test_the_joint_solve_opts_in_at_a_tenth_of_a_percent() -> None:
    assert JOINT_ACCEPT_MIP_GAP == 1e-3
