"""`solver.lp` on its failure and edge paths (#1837's coverage comparison).

The old solver files reached these only from tests that also test another
module (#773 and #1179 diagnostics, #1797's stochastic joint solve, #1805),
so they stay until those modules' own cutover; these are the v2 checks of
the same promises, by behaviour rather than by reading the source.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import patch

import highspy
import numpy as np
import pytest
from solver import lp
from solver.lp import BlendedOptions, CalibratedOptions, LexOptions, LPProblem


@pytest.fixture(autouse=True)
def _fresh_cooldown(monkeypatch):
    monkeypatch.setattr(lp, "_lex_calibration_failed_until", 0.0)


# --- #1805: a time-limited MIP keeps its incumbent only when asked to ------------


def _hard_knapsack(accept: float | None) -> LPProblem:
    """60 binaries under 40 knapsack rows: HiGHS cannot prove it in 0.3 s."""
    rng = np.random.default_rng(0)
    p = LPProblem()
    n, m = 60, 40
    for i in range(n):
        p.add_variable(f"x{i}", binary=True, cost=-float(rng.integers(1, 100)))
        p.set_secondary_cost(f"x{i}", 0.001)
    for _ in range(m):
        a = rng.integers(1, 100, n).astype(float)
        p.add_ub_constraint(
            {f"x{i}": float(a[i]) for i in range(n)}, float(a.sum() / 2)
        )
    p.time_limit_seconds = 0.3
    p.quiet_diagnostics = True
    p.accept_time_limited_mip_gap = accept
    return p


def test_a_timed_out_mip_is_an_error_unless_it_opts_in():
    """#1805: by default a MIP stopped on its time limit is an error, so the
    caller's fallback runs. Opted in, a feasible incumbent within the gap is
    kept, its binaries integral, and the pinned refresh after it gets its
    own time (the limit counts every run on the handle) so it completes. A
    gap wider than allowed is still refused."""
    default = _hard_knapsack(None).solve(options=BlendedOptions(blend_weight=1.0))
    assert (default.status, default.raw_status) == ("error", "Time limit reached")

    p = _hard_knapsack(1.0)
    kept = p.solve(options=BlendedOptions(blend_weight=1.0))
    assert kept.status == "optimal"
    xs = [p.value_of(kept, f"x{i}") for i in range(60)]
    assert all(abs(x - round(x)) < 1e-6 for x in xs) and sum(xs) > 0

    refused = _hard_knapsack(1e-12).solve(options=BlendedOptions(blend_weight=1.0))
    assert refused.status == "error"


# --- #773 tier 2: a failed phase 2 keeps the primary and the continuous tie-break --


def _switch_and_tie() -> LPProblem:
    """Exactly one of two equally priced switches, and a continuous tie the
    secondary breaks (x2 preferred)."""
    p = LPProblem()
    for i in range(2):
        p.add_variable(f"g{i}", binary=True, cost=2.0)
    p.add_eq_constraint({"g0": 1.0, "g1": 1.0}, 1.0)
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=1.0)
    p.set_secondary_cost("x2", -1.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0)
    return p


@contextlib.contextmanager
def _phase2_fails():
    """Run phase 2 for real, then report it non-optimal, as a timeout does."""
    real = lp._ensure_optimal_value

    def wrapper(h, *, phase="", problem=None, binary_cols=None):
        value = real(h, phase=phase, problem=problem, binary_cols=binary_cols)
        if phase == "phase2_secondary":
            raise ValueError("phase2_secondary did not reach optimal")
        return value

    with patch.object(lp, "_ensure_optimal_value", wrapper):
        yield


def test_a_failed_phase_two_pins_phase_one_and_solves_the_lp():
    """#773 tier 2: rather than drop the secondary channel, phase 1's binary
    assignment is pinned and phase 2 re-solved as an LP. The primary cost is
    the optimum, one switch is on, and the continuous tie still goes the
    secondary's way; no cooldown is armed, because it succeeded."""
    p = _switch_and_tie()
    with _phase2_fails():
        r = p.solve(options=LexOptions())
    assert r.status == "optimal"
    assert p.primary_cost_of(r) == pytest.approx(12.0)
    assert sorted(round(p.value_of(r, f"g{i}")) for i in range(2)) == [0, 1]
    assert p.value_of(r, "x2") == pytest.approx(10.0, abs=1e-4)
    assert lp._lex_calibration_failed_until == 0.0


def test_with_no_binaries_a_failed_phase_two_falls_back_to_the_plain_solve(caplog):
    """No binaries, nothing to pin: the failure propagates and the solve
    falls back to the plain one (#773), saying so."""
    p = LPProblem()
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=1.0)
    p.set_secondary_cost("x2", -1.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0)
    with caplog.at_level(logging.WARNING, logger=lp._LOGGER.name), _phase2_fails():
        r = p.solve(options=LexOptions())
    assert r.status == "optimal"
    assert r.objective == pytest.approx(10.0)
    assert any("#773" in rec.getMessage() for rec in caplog.records)


def test_a_highspy_that_refuses_a_mip_start_still_solves():
    """#773: the phase-1 point offered as a MIP start is best effort; a build
    that refuses it solves the same problem to the same answer."""
    p = _switch_and_tie()
    normal = p.solve(options=LexOptions())

    def refuse(_self, _seed):
        raise RuntimeError("setSolution not supported")

    with patch.object(highspy.Highs, "setSolution", refuse):
        seeded = _switch_and_tie().solve(options=LexOptions())
    assert seeded.status == normal.status == "optimal"
    assert seeded.objective == pytest.approx(normal.objective)


def test_pinning_no_binaries_changes_nothing():
    """#773: with no binary columns there is nothing to pin."""
    calls = []
    h = SimpleNamespace(
        changeColIntegrality=lambda *a: calls.append(a),
        changeColBounds=lambda *a: calls.append(a),
    )
    lp._pin_binaries_to_values(h, [], [0.4, 1.0])
    assert calls == []


# --- #1179: calibration's three outcomes, each named -----------------------------


def _near_tie(secondary_on_cheaper: float) -> LPProblem:
    """x1 is 0.1c cheaper on the primary; the secondary charges x1."""
    p = LPProblem()
    p.add_variable("x1", ub=1.0, cost=1.0)
    p.add_variable("x2", ub=1.0, cost=1.001)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 1.0)
    p.set_secondary_cost("x1", secondary_on_cheaper)
    return p


def test_calibration_finds_the_largest_weight_that_keeps_the_primary():
    """#1179: the largest weight (1e-1) would buy x2 and lose 0.1c of
    primary, beyond the tolerance; the smallest keeps it; the bisection
    between them lands on a weight that keeps it, with no fallback."""
    p = _near_tie(10.0)
    r = p.solve(options=CalibratedOptions())
    assert r.status == "optimal"
    # Kept within the calibration tolerance (1e-4 of the primary), not
    # moved to x2, which would cost 0.1c more.
    assert p.primary_cost_of(r) <= 1.0 + 1e-4 + 1e-9
    assert p.value_of(r, "x2") < 0.5
    assert not r.calibration_min_weight_fallback
    assert r.calibration_fallback_reason is None


@contextlib.contextmanager
def _probes_fail(which):
    """Report the calibration probes whose 0-based order is in `which` as not
    solved (the largest weight is probed first)."""
    state = {"probe": False, "n": -1}
    real_call = lp._timed_lp_call
    real_status = highspy.Highs.getModelStatus

    @contextlib.contextmanager
    def timed(h, label, **kw):
        state["probe"] = label == "primary_acceptable_probe"
        if state["probe"]:
            state["n"] += 1
        with real_call(h, label, **kw):
            yield

    def status(self):
        if state["probe"] and (which is None or state["n"] in which):
            return highspy.HighsModelStatus.kNotset
        return real_status(self)

    with (
        patch.object(lp, "_timed_lp_call", timed),
        patch.object(highspy.Highs, "getModelStatus", status),
    ):
        yield


def test_when_only_the_largest_weight_fails_the_bisection_finds_one_that_holds():
    """#1179: the largest weight rejected and the smallest accepted, the
    log-weight bisection between them runs and lands on a weight that keeps
    the primary; nothing falls back."""
    p = _near_tie(10.0)
    with _probes_fail({0}):
        r = p.solve(options=CalibratedOptions())
    assert r.status == "optimal"
    assert p.primary_cost_of(r) <= 1.0 + 1e-4 + 1e-9
    assert not r.calibration_min_weight_fallback


def test_a_probe_that_does_not_solve_is_named_a_symptom():
    """#1179: when the calibration probes themselves do not reach optimal,
    the minimum weight is used and the fallback is named a symptom of a model
    already failing, `probe_not_optimal`, not a cause."""
    with _probes_fail(None):
        r = _near_tie(10.0).solve(options=CalibratedOptions())
    assert r.calibration_min_weight_fallback
    assert r.calibration_fallback_reason == "probe_not_optimal"


# --- #1179: the HiGHS snapshot never breaks a failing cycle ----------------------


def test_the_highs_snapshot_survives_any_handle():
    """#1179: no getInfo, or one that raises, is an empty snapshot; a field
    this build lacks, a None and a non-number are absent, never a zero."""

    def boom():
        raise RuntimeError("no info")

    assert lp._highs_info_snapshot(SimpleNamespace(getInfo=boom)) == {}
    assert lp._highs_info_snapshot(SimpleNamespace()) == {}
    info = SimpleNamespace(
        simplex_iteration_count=42, mip_gap=None, mip_node_count="many"
    )
    snap = lp._highs_info_snapshot(SimpleNamespace(getInfo=lambda: info))
    assert snap == {"simplex_iteration_count": 42.0}


def test_a_quiet_problem_never_dumps_its_model(monkeypatch):
    """#1797: the stochastic joint solve is quiet; it writes no MPS file."""
    monkeypatch.setattr(lp, "_diagnostics_quiet", lambda: True)
    assert lp._dump_failing_model(object(), "any_phase") is None


# --- #1797: absorbing one problem into another -----------------------------------


def test_absorb_refuses_what_would_collide():
    """#1797: an unprefixed absorb is only into an empty problem (it must
    reproduce the other exactly), and a name already registered is refused."""
    other = LPProblem()
    other.add_variable("x", ub=1.0, cost=1.0)
    target = LPProblem()
    target.add_variable("y", ub=1.0)
    with pytest.raises(ValueError, match="empty problem"):
        target.absorb(other, prefix="", cost_scale=1.0)
    target.add_variable("s_x", ub=1.0)
    with pytest.raises(ValueError, match="already registered"):
        target.absorb(other, prefix="s_", cost_scale=1.0)
    joint = LPProblem()
    joint.absorb(other, prefix="", cost_scale=1.0)
    assert joint.has_variable("x") and not joint.has_variable("y")
