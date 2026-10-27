"""A MIP that stops on its time limit keeps the incumbent when the problem opts
in and the gap is small enough (#1805) (#1837).
"""

from __future__ import annotations

import numpy as np
from solver import lp as _solver_lp
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


def test_the_joint_solve_opts_in_at_a_tenth_of_a_percent():
    # test_the_joint_solve_opts_in_at_a_tenth_of_a_percent
    _solver_lp._lex_calibration_failed_until = 0.0
    assert JOINT_ACCEPT_MIP_GAP == 1e-3

    # test_the_kept_incumbent_gets_time_to_re_solve_its_pinned_lp: The time limit counts the whole Highs object's run, already spent by
    _solver_lp._lex_calibration_failed_until = 0.0
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


def test_a_wall_deadline_bounds_every_run_of_the_solve(monkeypatch):
    """#2047, part 2 (Mark Purcell): the stochastic joint call's limit
    bounds the whole solve, not each HiGHS run. A kept incumbent's pinned-LP
    refresh used to get 5 s past the limit (#1805); with a deadline every
    run is cut to what is left of it, and a spent one fails the solve so
    the cycle falls back to the main plan."""
    import time

    from solver import lp

    _solver_lp._lex_calibration_failed_until = 0.0
    runs = []
    real = lp._bound_by_wall_deadline

    def bound(h):
        _s, before = h.getOptionValue("time_limit")
        real(h)
        _s, after = h.getOptionValue("time_limit")
        mip = any(int(v) for v in h.getLp().integrality_)
        runs.append((float(before), float(after), mip, h.getRunTime()))

    monkeypatch.setattr(lp, "_bound_by_wall_deadline", bound)
    p = _hard_knapsack(1.0)
    started = time.monotonic()
    p.wall_deadline = started + 0.6
    assert p.solve().status == "optimal"
    # The MIP first, then the pinned LP the kept incumbent re-solves.
    assert [mip for *_, mip, _rt in runs][:1] == [True]
    assert len(runs) >= 2
    for _before, after, mip, run_time in runs:
        cap = 0.6 if mip else run_time + 0.6
        assert after <= cap + 1e-9
    # The plant: the refresh asked for 5 s more than the deadline left, and
    # the bound cut it.
    assert any(before > after + 1.0 for before, after, *_ in runs)

    # A spent deadline fails the solve, as the joint call's fallback expects.
    p = _hard_knapsack(1.0)
    p.wall_deadline = time.monotonic() - 1.0
    result = p.solve()
    assert (result.status, result.raw_status) == ("error", "wall_deadline_spent")
    # And none, as every other caller has, changes nothing.
    runs.clear()
    assert _hard_knapsack(1.0).solve().status == "optimal"
    assert all(before == after for before, after, *_ in runs)
