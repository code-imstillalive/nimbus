"""A MIP that stops on its time limit keeps the incumbent when the problem opts
in and the gap is small enough (#1805) (#1837).
"""

from __future__ import annotations

import numpy as np
from solver.lp import LPProblem
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


def test_the_joint_solve_opts_in_at_a_tenth_of_a_percent() -> None:
    assert JOINT_ACCEPT_MIP_GAP == 1e-3


def test_the_kept_incumbent_gets_time_to_re_solve_its_pinned_lp() -> None:
    """The time limit counts the whole Highs object's run, already spent by
    the MIP; without fresh time the pinned LP that recovers the duals stops
    at once, unsolved."""
    import contextlib
    from unittest.mock import patch

    from solver import lp

    seen = []
    real = lp._timed_lp_call

    @contextlib.contextmanager
    def timed(h, label, **kw):
        with real(h, label, **kw):
            yield
        if label == "mip_pin_reoptimise":
            seen.append(h.modelStatusToString(h.getModelStatus()))

    with patch.object(lp, "_timed_lp_call", timed):
        assert _hard_knapsack(1.0).solve().status == "optimal"
    assert seen == ["Optimal"]
