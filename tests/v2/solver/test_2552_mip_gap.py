"""#2552: a main-plan MIP that stalls proving its last fraction of a cent
stops and keeps its plan.

Saved slow main solves had the plan at the root node in about a second and
then spent up to 60 s proving the last 0.0123% of a $509 objective without
improving it. On the test box's saved model, a stop at 10 s within a 2e-4 gap
returned the identical objective (10.8 s on HiGHS 1.15.1, 11.4 s on 1.16.0).
A solve that finishes before the stall time never meets the rule, and only
the main plan carries it.
"""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import highspy
from solver import lp, network
from solver.lp import LPProblem


def _recording_highs(monkeypatch, calls: list):
    real = highspy.Highs

    class Recording(real):  # type: ignore[misc, valid-type]
        def setCallback(self, fn, user):
            calls.append(("set", fn))
            return super().setCallback(fn, user)

        def startCallback(self, kind):
            calls.append(("start", kind))
            return super().startCallback(kind)

        def stopCallback(self, kind):
            calls.append(("stop", kind))
            return super().stopCallback(kind)

    monkeypatch.setattr(lp.highspy, "Highs", Recording)


def _small_mip(stall: bool) -> LPProblem:
    p = LPProblem()
    on = p.add_variable("on", binary=True, cost=-3.0)
    x = p.add_variable("x", ub=10.0, cost=-1.0)
    p.add_ub_constraint({x: 1.0, on: 4.0}, 8.0)
    if stall:
        p.stall_accept_after_seconds = lp.MAIN_PLAN_STALL_SECONDS
        p.stall_accept_mip_gap = lp.MAIN_PLAN_STALL_MIP_GAP
    return p


def test_only_a_problem_with_the_stall_rule_gets_the_interrupt_and_it_fires_only_when_stalled_and_close(
    monkeypatch,
):
    calls: list = []
    _recording_highs(monkeypatch, calls)

    assert _small_mip(stall=False).solve().status == "optimal"
    assert calls == []

    result = _small_mip(stall=True).solve()
    assert result.status == "optimal"  # a fast solve is untouched
    interrupt = highspy.cb.HighsCallbackType.kCallbackMipInterrupt
    # Started for the main run and stopped before anything else runs on `h`
    # (the pinned-LP refresh included).
    assert [c for c in calls if c[0] in ("start", "stop")] == [
        ("start", interrupt),
        ("stop", interrupt),
    ]

    check = next(c[1] for c in calls if c[0] == "set")
    after, gap = lp.MAIN_PLAN_STALL_SECONDS, lp.MAIN_PLAN_STALL_MIP_GAP
    for running, mip_gap, stops in [
        (after + 1.0, gap * 0.6, True),  # stalled and close: stop
        (after - 1.0, gap * 0.6, False),  # close but still early
        (after + 1.0, gap * 3.0, False),  # stalled but not close enough
    ]:
        data_in = SimpleNamespace(user_interrupt=False)
        check(
            None,
            "",
            SimpleNamespace(running_time=running, mip_gap=mip_gap),
            data_in,
            None,
        )
        assert data_in.user_interrupt is stops, (running, mip_gap)


def test_an_interrupted_main_plan_keeps_a_feasible_incumbent_within_the_gap():
    def stopped(gap: float, feasible: bool = True):
        info = SimpleNamespace(
            primal_solution_status=int(
                highspy.SolutionStatus.kSolutionStatusFeasible
                if feasible
                else highspy.SolutionStatus.kSolutionStatusNone
            ),
            mip_gap=gap,
        )
        return SimpleNamespace(
            getModelStatus=lambda: highspy.HighsModelStatus.kInterrupt,
            getInfo=lambda: info,
            getRunTime=lambda: 10.8,
        )

    assert lp._accept_stalled_incumbent(stopped(1.23e-4), _small_mip(stall=True))
    assert not lp._accept_stalled_incumbent(stopped(5e-4), _small_mip(stall=True))
    assert not lp._accept_stalled_incumbent(
        stopped(1e-4, feasible=False), _small_mip(stall=True)
    )
    assert not lp._accept_stalled_incumbent(stopped(1e-4), _small_mip(stall=False))


def test_the_main_plan_is_the_problem_that_carries_the_rule():
    source = inspect.getsource(network._build_plan_once)
    assert "p.stall_accept_after_seconds = MAIN_PLAN_STALL_SECONDS" in source
    assert "p.stall_accept_mip_gap = MAIN_PLAN_STALL_MIP_GAP" in source
    assert lp.MAIN_PLAN_STALL_SECONDS == 10.0
    assert lp.MAIN_PLAN_STALL_MIP_GAP == 2e-4


def test_2566_a_stall_accepted_main_plan_is_not_a_773_slow_call_alarm(
    monkeypatch, caplog
):
    # Live 20 Nov: the rule stopped a 15.8 s main solve at mip_gap=0.0 and kept
    # the plan, but the slow-call diagnostic still logged a #773 WARNING and
    # dumped the model, because "Interrupted by user" was not a status it knew.
    dumps: list = []
    monkeypatch.setattr(
        lp, "_dump_failing_model", lambda h, label, **_k: dumps.append(label) or "x"
    )
    monkeypatch.setattr(lp, "_diagnostics_quiet", lambda: False)

    def interrupted(gap: float):
        info = SimpleNamespace(
            simplex_iteration_count=121846,
            mip_node_count=3926,
            mip_gap=gap,
            primal_solution_status=int(highspy.SolutionStatus.kSolutionStatusFeasible),
        )
        return SimpleNamespace(
            getInfo=lambda: info,
            getModelStatus=lambda: highspy.HighsModelStatus.kInterrupt,
            modelStatusToString=lambda _s: "Interrupted by user",
        )

    def run(h, stall_gap):
        clock = iter([0.0, 15.8])
        monkeypatch.setattr(lp, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        with lp._timed_lp_call(
            h, "primary_minimize", n_vars=9750, n_binary=995, stall_accept_gap=stall_gap
        ):
            pass

    caplog.set_level(logging.DEBUG, logger=lp._LOGGER.name)
    run(interrupted(0.0), lp.MAIN_PLAN_STALL_MIP_GAP)
    assert dumps == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "Stall accepted" in caplog.text

    # The same interrupt outside the rule's gap, or with no rule, is still an
    # alarm with a model dump.
    caplog.clear()
    run(interrupted(5e-3), lp.MAIN_PLAN_STALL_MIP_GAP)
    run(interrupted(0.0), None)
    assert dumps == ["slow_primary_minimize", "slow_primary_minimize"]
    warned = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "#773 diag" in r.getMessage()
    ]
    assert len(warned) == 2

    # The main plan's timed call is the one that passes its rule's gap.
    source = inspect.getsource(lp)
    call = source[source.index('"primary_minimize"') :][:200]
    assert "stall_accept_gap=" in call
