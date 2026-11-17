"""#2552: a MIP plan stops once it is within 0.02% (or $0.10) of proven best.

HiGHS's own default relative gap (1e-4) held saved slow main solves for the
whole 60 s budget, proving the last 0.0123% of a $509 objective without ever
improving the plan (test install, 17 Nov 2026: identical objective in 1.1 s
with the gap at 2e-4, on HiGHS 1.15.1 and 1.16.0 alike). Every solve now
passes both gaps to HiGHS explicitly, so a library default can't bring the
stall back.
"""

from __future__ import annotations

import highspy
from solver import lp
from solver.lp import LPProblem


def test_every_solve_passes_the_plan_gaps_to_highs(monkeypatch):
    seen: dict[str, object] = {}
    real = highspy.Highs

    class Recording(real):  # type: ignore[misc, valid-type]
        def setOptionValue(self, name, value):
            seen[name] = value
            return super().setOptionValue(name, value)

    monkeypatch.setattr(lp.highspy, "Highs", Recording)

    p = LPProblem()
    on = p.add_variable("on", binary=True, cost=-3.0)
    x = p.add_variable("x", ub=10.0, cost=-1.0)
    p.add_ub_constraint({x: 1.0, on: 4.0}, 8.0)
    result = p.solve()

    assert result.status == "optimal"
    assert seen.get("mip_rel_gap") == lp.MIP_REL_GAP == 2e-4
    assert seen.get("mip_abs_gap") == lp.MIP_ABS_GAP == 0.10
