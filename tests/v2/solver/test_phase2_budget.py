"""Speed, A (household, 22 Oct 2026, after HAEO 0.5.0rc1): phase 2, the
tie-break among plans phase 1 proved equally cheap, has its own budget.
Spent, the tie-break finishes as an LP on the better of phase 1's switch
choices and the best phase 2 had found. The primary cost is unchanged by
construction; these check that, and that nothing is logged as a failure.

A problem small enough for a test finishes phase 2 before HiGHS looks at
the clock, so the budget is spent here by reporting it, after the real
phase 2 has run."""

from __future__ import annotations

import contextlib
import logging
from unittest.mock import patch

import pytest
from solver import lp
from solver.lp import LexOptions, LPProblem


def _switch_and_tie(prefer_g1: float = 0.0) -> LPProblem:
    """Exactly one of two equally priced switches; the secondary prefers g1
    (or g0, negative), and a continuous tie it breaks (x2 preferred)."""
    p = LPProblem()
    for i in range(2):
        p.add_variable(f"g{i}", binary=True, cost=2.0)
    p.add_eq_constraint({"g0": 1.0, "g1": 1.0}, 1.0)
    if prefer_g1:
        p.set_secondary_cost("g1", -prefer_g1)
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=1.0)
    p.set_secondary_cost("x2", -1.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0)
    return p


@contextlib.contextmanager
def _budget_spent():
    real = lp._ensure_optimal_value

    def wrapper(h, *, phase="", problem=None, binary_cols=None, budgeted=False):
        value = real(
            h, phase=phase, problem=problem, binary_cols=binary_cols, budgeted=budgeted
        )
        if phase == "phase2_secondary":
            assert budgeted
            raise lp._Phase2BudgetSpent("phase2_secondary used its budget")
        return value

    with patch.object(lp, "_ensure_optimal_value", wrapper):
        yield


@contextlib.contextmanager
def _points(p: LPProblem, phase1: str, found: str | None):
    """Phase 1 chose switch `phase1`; phase 2 had found `found` (or none)."""

    def point(h, on):
        vals = list(h.getSolution().col_value)
        for g in ("g0", "g1"):
            vals[p._var_index[g]] = 1.0 if g == on else 0.0
        return vals

    with (
        patch.object(lp, "_capture_integral_point", lambda h: point(h, phase1)),
        patch.object(
            lp, "_feasible_incumbent", lambda h: point(h, found) if found else None
        ),
    ):
        yield


def test_a_spent_budget_keeps_the_cost_and_the_continuous_tie_break(caplog):
    reference = _switch_and_tie()
    full = reference.solve(options=LexOptions())
    p = _switch_and_tie()
    with (
        caplog.at_level(logging.DEBUG, logger=lp._LOGGER.name),
        _budget_spent(),
    ):
        r = p.solve(options=LexOptions())
    assert r.status == "optimal"
    assert p.primary_cost_of(r) == pytest.approx(reference.primary_cost_of(full))
    assert p.value_of(r, "x2") == pytest.approx(10.0, abs=1e-4)
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert any("used its" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize(
    ("prefer_g1", "phase1", "found", "kept"),
    [
        (5.0, "g0", "g1", "g1"),  # what phase 2 found is better: kept
        (-5.0, "g0", "g1", "g0"),  # phase 1's is better: kept
        (5.0, "g0", None, "g0"),  # nothing found: phase 1's, as #773 tier 2
    ],
)
def test_the_better_switch_assignment_is_kept(prefer_g1, phase1, found, kept):
    p = _switch_and_tie(prefer_g1)
    with _budget_spent(), _points(p, phase1, found):
        r = p.solve(options=LexOptions())
    assert r.status == "optimal"
    assert round(p.value_of(r, kept)) == 1
    assert p.primary_cost_of(r) == pytest.approx(12.0)


def test_the_budget_is_put_back_for_the_phases_after_it(monkeypatch):
    monkeypatch.setattr(lp, "PHASE2_SECONDARY_BUDGET_SECONDS", 2.5)
    seen = []
    real = lp._Phase2Budget.restore

    def restore(self):
        seen.append(self._h.getOptionValue("time_limit")[1])
        real(self)
        seen.append(self._h.getOptionValue("time_limit")[1])

    with patch.object(lp._Phase2Budget, "restore", restore), _budget_spent():
        _switch_and_tie().solve(options=LexOptions())
    assert seen[:2] == [
        pytest.approx(2.5),
        pytest.approx(lp.DEFAULT_TIME_LIMIT_SECONDS),
    ]


def test_without_switches_there_is_no_budget():
    """A pure LP has no phase-2 search to bound."""
    p = LPProblem()
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=1.0)
    p.set_secondary_cost("x2", -1.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0)
    with patch.object(lp, "_Phase2Budget", side_effect=AssertionError("budgeted")):
        r = p.solve(options=LexOptions())
    assert r.status == "optimal"
    assert p.value_of(r, "x2") == pytest.approx(10.0, abs=1e-4)


class _Handle:
    def __init__(self, status):
        self._status = status

    def getModelStatus(self):
        return self._status

    def modelStatusToString(self, status):
        return "Time limit reached"


def test_a_spent_budget_is_labelled_and_a_real_timeout_is_not():
    timed_out = _Handle(lp.highspy.HighsModelStatus.kTimeLimit)
    assert lp._status_label(timed_out, budgeted=True) == lp._BUDGET_SPENT
    assert lp._status_label(timed_out, budgeted=False) == "Time limit reached"


def test_a_spent_budget_does_not_raise_the_solve_summary_to_a_warning(caplog):
    """The per-solve breakdown warns for a slow or failed phase; phase 2 on
    its budget is neither, so the summary stays at DEBUG."""
    with (
        caplog.at_level(logging.DEBUG, logger=lp._LOGGER.name),
        lp._phase_breakdown(),
    ):
        lp._phase_record(("lex_phase:phase1_primary", 1.0, 10, 1, 0.0, "Optimal"))
        lp._phase_record(
            ("lex_phase:phase2_secondary", 5.1, 99, 0, 0.1, lp._BUDGET_SPENT)
        )
    summary = [r for r in caplog.records if "phase breakdown" in r.getMessage()]
    assert summary and all(r.levelno == logging.DEBUG for r in summary)
