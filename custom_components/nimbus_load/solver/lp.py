"""A small, general-purpose linear program solver -- a thin builder layer
over `highspy` (HiGHS), the same real compiled LP/MIP solver the sibling
HAEO integration already uses via `from highspy import Highs` (see the
sibling repo's own haeo_repo/custom_components/haeo/core/model/network.py).

## Why this changed (2026-08-18)

This module used to be a genuine from-scratch two-phase revised-simplex-
on-a-dense-tableau implementation, deliberately avoiding highspy/scipy/
PuLP on the assumption (written into this docstring at the time) that no
compiled LP library had "confirmed wheel availability... inside the HA
container it deploys into." That assumption was correct about the
CONTAINER (HA's own custom_component runtime, which really doesn't have
a C compiler) -- but this solver was never actually running there.
`nimbus_solver_forecast_writer.py` is a plain HOST cron script (see its
own module docstring), a completely different Python environment from
HA's container, with its own separate `pip`.

Once the real Nimbus Solver horizon grew to a genuine ~365-period,
several-thousand-variable problem (2026-08-16's 96h tiered grid), the
from-scratch dense simplex became a real, live bottleneck (10-11 minute
solves against a live NUC, confirmed via this project's own repeated
production log evidence) even after a hybrid Dantzig/Bland pivot rule
fix resolved its earlier non-convergence crash the same day. Directly
testing `highspy` against the REAL host environment (not assumed,
confirmed live: `sudo apt install python3-pip && pip install
--break-system-packages highspy` installed a real matching manylinux
wheel for this project's own NUC hosts, cp312-x86_64, and solved a
synthetic problem at the real production scale -- ~4000 variables,
~1500 constraints -- in ~0.03s) showed the original constraint never
actually applied to this specific script at all.

This module keeps the exact same public API (`LPProblem`, `LPResult`,
`add_variable`/`set_cost`/`add_ub_constraint`/`add_eq_constraint`/
`solve`/`value_of`/`values_of`) that network.py and every existing test
already depend on -- only the INTERNAL solve mechanism changed, from a
from-scratch simplex to a thin highspy translation layer. No caller
needed to change, and none of the existing tests needed to change either
(they only ever exercised this public API, never the old simplex
internals directly).

Problem form accepted (the "natural" LP form -- callers never build
standard-form arrays themselves):

    minimize    c^T x
    subject to  A_ub x <= b_ub      (any number of rows, may be empty)
                A_eq x  = b_eq      (any number of rows, may be empty)
                lb <= x <= ub       (per-variable, -inf/+inf allowed)

Free variables (lb=-inf) are passed straight through to highspy as a
genuinely unbounded-below column (`-highspy.kHighsInf`) -- HiGHS
supports this natively, so the manual positive/negative variable-
splitting technique the old from-scratch solver needed is no longer
necessary at all; this is a real simplification, not just a swap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import highspy
import numpy as np
from numpy.typing import NDArray

_LOGGER = logging.getLogger(__name__)

# nimbus issue #696, Stage 1: a real primary/secondary objective
# architecture, ported from the sibling HAEO integration's own
# `custom_components/haeo/core/model/network.py` (`LexOptions` /
# `BlendedOptions` / `CalibratedOptions`, fetched and read in full via
# `gh api repos/purcell-lab/haeo/contents/...` before writing this --
# not reconstructed from a summary). Motivation: this project has grown
# FOUR independent hand-tuned epsilon tie-break mechanisms (network.py's
# proximal_weight, smoothness_weight, battery_charge_earliness_budget_kw,
# adequacy_earliness_budget_kw), each summed directly into the one real
# cost objective and each individually verified "far enough below real
# tariff granularity" -- but nothing structurally guarantees that stays
# true as more get added, or that they never compose badly in some
# untested combination. HAEO's answer: every cost contribution declares
# itself PRIMARY (a real economic cost) or SECONDARY (a tie-break
# preference among primary-optimal solutions), and the solve mode
# decides how secondary influences the answer:
#
# - `LexOptions`: genuine three-phase lexicographic optimization.
#   Phase 1 minimizes primary alone. Phase 2 minimizes secondary with
#   primary HARD-constrained (`<=` its own phase-1 optimal value) --
#   secondary can never make primary worse, not even by an epsilon.
#   Phase 3 re-minimizes primary with a tiny relative epsilon slack on
#   secondary, purely to recover clean duals/reduced costs at (or
#   negligibly close to) the phase-2 point without perturbing it.
# - `BlendedOptions`: a plain single-solve weighted sum, `primary +
#   blend_weight * secondary` -- the exact architecture this project's
#   own existing tie-break mechanisms already use today, just now named
#   and made an explicit, opt-in solve mode rather than baked silently
#   into every `set_cost()` call.
# - `CalibratedOptions`: HAEO's own default. Runs phases 1+2 once (the
#   real lex optimum), then binary-searches log10 space for the LARGEST
#   blend weight whose single-solve primary cost still stays within
#   `calibration_tolerance` of that true optimum, steps back one log10
#   decade for safety margin, then does one final blended solve at that
#   weight. A searched-safe magnitude, not a hand-picked one -- the
#   real answer to "how do N independent epsilons compose safely" this
#   project's own tie-break mechanisms have so far solved by manual
#   verification alone.
#
# `LPProblem.solve(options=None)` (the default) is BYTE-IDENTICAL to
# this module's own pre-#696 behavior -- a single blended solve using
# only `_cost` (secondary is never even read). No existing caller or
# test needed to change for this stage; migrating network.py's own
# four mechanisms onto `set_secondary_cost()` is deliberately a
# SEPARATE, later PR (#696's own Stage 2), with its own dedicated
# devhub verification, once this architecture is proven correct here
# in isolation first.
#
# Deliberate scope cut vs. HAEO: no `SimplexTuning`/HiGHS solver-option
# dataclasses ported -- `_solve_highs()` already sets its own solver
# options (time_limit, output_flag) directly, unrelated to the
# primary/secondary architecture itself. Also deliberately NOT ported:
# HAEO's cross-call `_calibrated_weight` caching on a persistent
# `Network` object -- Nimbus's own `LPProblem` is built fresh every
# solve (no persistent object across solve cycles), so `CalibratedOptions`
# here re-runs the full lex+calibration search on EVERY call rather than
# reusing a cached weight. A future optimization (network.py threading a
# previous solve's calibrated weight through, mirroring how it already
# threads a previous PLAN through for proximal regularization) is
# possible but out of scope for this stage -- tracked in #696 if real
# solve-latency measurements ever call for it.
_CAL_LOG_LO: float = -12.0
_CAL_LOG_HI: float = -1.0
_CAL_MAX_STEPS: int = 40
_CAL_CONVERGENCE: float = 0.01
_CAL_MARGIN: float = 1.0


@dataclass(frozen=True)
class LexOptions:
    """Three-phase lexicographic optimization. See this module's own
    top-of-file comment for the full phase-by-phase description."""

    mode: Literal["lex"] = "lex"


@dataclass(frozen=True)
class BlendedOptions:
    """Single-solve weighted sum: primary + blend_weight * secondary.
    The exact architecture this project's own pre-#696 tie-break
    mechanisms already used, now an explicit opt-in mode."""

    mode: Literal["blended"] = "blended"
    blend_weight: float = 1e-3


@dataclass(frozen=True)
class CalibratedOptions:
    """Two-phase lex once, then a calibrated blended fast path -- see
    this module's own top-of-file comment for the full mechanism.
    `calibration_tolerance` is the max RELATIVE degradation of the
    primary objective the calibrated weight is allowed to risk,
    relative to the true lex-optimal primary value."""

    mode: Literal["calibrated"] = "calibrated"
    calibration_tolerance: float = 1e-4


SolveOptions = LexOptions | BlendedOptions | CalibratedOptions

# nimbus issue #356: bounds a genuinely stuck solve (see _solve_highs's own
# comment at its call site for the full reasoning) -- not a performance
# tuning knob, a safety backstop.
DEFAULT_TIME_LIMIT_SECONDS: float = 60.0

# nimbus issue #490: below this, a ranging interval is treated as
# genuinely zero-width (a real tie in the optimal basis, HAEO #465's own
# "saturated" concept) rather than a tiny nonzero float HiGHS's own
# numerics happened to report -- same tolerance #465 itself uses.
_RANGING_DEGENERATE_TOL = 1e-9


@dataclass(frozen=True)
class RangingRecord:
    """One HiGHS ranging entry (nimbus issue #490) -- the absolute
    bound/RHS/cost value at which the current optimal basis stops being
    optimal (`value`), the objective value AT that point (`objective`),
    and which variable would enter/leave the basis there (`in_var`/
    `out_var`, `None` for HiGHS's own -1 "no variable" sentinel -- see
    `_var_name_or_none()`)."""

    value: float
    objective: float
    in_var: str | None
    out_var: str | None


@dataclass(frozen=True)
class SweepRangingStep:
    """One step of `LPResult.sweep_cost_with_ranging()` (nimbus issue
    #676) -- `value` is the swept variable's own resulting value at this
    step's cost coefficient, same meaning as one entry of plain
    `sweep_cost()`'s own return list. `cost_dn`/`cost_up` are that SAME
    variable's own real HiGHS cost-ranging AT this exact re-solved basis
    -- the genuine, exact interval (not a sampled bracket) the swept cost
    coefficient could move through in either direction before `value`
    itself would change. Both `None` when ranging came back invalid at
    this specific step (HiGHS reports a real tie/degenerate basis, or
    ranging itself failed) -- represented honestly rather than papered
    over, same convention `LPResult.bound_headroom()` already uses for
    the same situation.

    Values here are in the LP's own raw internal cost-coefficient units
    (whatever `sweep_cost_with_ranging()`'s own `costs` argument was
    expressed in) -- this module has no notion of $/kWh vs. period-scaled
    $ vs. any other domain unit, same as every other ranging field on
    this class. A caller translating this into a real price (e.g.
    network.py's offer curve, dividing by that period's own `hours[t]`
    the same way nimbus issue #662 already established for the plain
    per-period dual) is responsible for that conversion itself.
    """

    value: float
    cost_dn: RangingRecord | None
    cost_up: RangingRecord | None


@dataclass(frozen=True)
class Headroom:
    """Real room to move, in each direction, before the optimal basis
    changes (nimbus issue #490) -- `down`/`up` are always >= 0.0.
    `degenerate=True` when either side was clamped up from a genuinely
    zero-width (< 1e-9) interval: the current solution sits at a real
    tie in the LP, so that side's own headroom is honestly "none right
    now", not a rounding artifact. HAEO #465 silently treats this same
    condition as "saturated" and falls back to a different number;
    Nimbus reports it explicitly instead, exactly as nimbus issue
    #489's own research doc calls for."""

    down: float
    up: float
    degenerate: bool


def _headroom_from(down_raw: float, up_raw: float) -> Headroom:
    degenerate = down_raw < _RANGING_DEGENERATE_TOL or up_raw < _RANGING_DEGENERATE_TOL
    down = 0.0 if down_raw < _RANGING_DEGENERATE_TOL else down_raw
    up = 0.0 if up_raw < _RANGING_DEGENERATE_TOL else up_raw
    return Headroom(down=down, up=up, degenerate=degenerate)


@dataclass(frozen=True)
class LPResult:
    """Outcome of solve(). `status` is one of "optimal", "infeasible",
    "unbounded", or "error" -- never an exception for a genuinely
    infeasible/unbounded problem, since both are real, expected outcomes a
    caller (network.py) needs to handle explicitly, not treat as a crash.
    `x`/`objective` are only meaningful when status == "optimal".

    nimbus issue #356 (Mark Purcell): "error" (2026-09-04) is a distinct
    outcome from "infeasible" -- HiGHS can report several genuine
    SOLVER-level failures (hit the time limit, hit an iteration/solution
    limit, an internal model/solve error, or a plain "unknown" status) that
    are NOT the same thing as a model that was actually proven infeasible.
    Before this fix, every one of those was silently collapsed into
    `status="infeasible"`, misleading every downstream consumer (and any
    operator reading a log) into thinking the model itself has no feasible
    dispatch, when the real problem is that the SOLVER gave up/timed out
    on a model that may well have a feasible answer. Confirmed safe to add
    as a genuinely new value (not just a naming change): every existing
    consumer of `.status`/`Plan.status` in this repo (network.py,
    stochastic.py, solver_writer.py) only ever checks `== "optimal"` or
    `!= "optimal"`, never `== "infeasible"` specifically -- so introducing
    "error" changes no existing control-flow branch, it only adds
    diagnostic precision for whichever branch already runs for "not
    optimal". `raw_status` carries HiGHS's own status name (e.g.
    "kTimeLimit") whenever status is "error", so a caller/log line can name
    the real cause instead of sending an operator hunting for a modeling
    bug that doesn't exist.

    `duals` (2026-08-18): one entry per named constraint ROW, keyed by
    whatever `name=` was passed to add_ub_constraint()/add_eq_constraint()
    (or an auto-generated `ub_{i}`/`eq_{i}` fallback for unnamed rows --
    every row always gets an entry, naming is purely for readability, never
    required for coverage). The dual value is the marginal change in the
    objective per unit of RHS relaxation -- e.g. a per-period power-balance
    row's dual is literally that period's real-time shadow price of energy.

    `reduced_costs`: one entry per VARIABLE, keyed by variable name. Only
    meaningful (nonzero) when that variable is sitting AT one of its own
    bounds in the optimal solution -- this is the direct answer to "is this
    specific cap actually binding right now" for anything modeled as a
    variable bound rather than a separate constraint row (e.g. a grid
    export limit or a battery max-power cap set via add_variable(ub=...)
    rather than an explicit row).

    Both are empty dicts (never None) on a non-optimal result, matching
    this class's own existing "x/objective only meaningful when optimal"
    convention -- an empty dict is a safe, iterable default a caller can
    treat uniformly instead of needing an extra None-check.
    """

    status: str
    x: NDArray[np.float64] | None = None
    objective: float | None = None
    iterations: int = 0
    duals: dict[str, float] = field(default_factory=dict)
    reduced_costs: dict[str, float] = field(default_factory=dict)
    raw_status: str | None = None

    # nimbus issue #490 (Signals 1/7 of #489): HiGHS ranging, opt-in via
    # LPProblem.solve(ranging=True) -- see this module's own docstring on
    # `solve()` for the full mechanism. `ranging_valid` is None when
    # ranging wasn't requested at all (the caller never asked, so there's
    # nothing to say either way -- distinct from HAVING asked and HiGHS
    # saying no), False when HiGHS itself reports `valid=False` (a
    # genuinely non-optimal status, or ranging requested on a MIP whose
    # pinned-relax pass didn't reach kOptimal), True otherwise. Every
    # ranging dict below is empty (never populated with zeros/garbage)
    # whenever ranging_valid is not True -- same "represent honestly"
    # convention `duals`/`reduced_costs` already use for a non-optimal
    # result.
    ranging_valid: bool | None = None
    # Keyed by VARIABLE name, one RangingRecord per variable -- how far
    # that variable's own bound (col_bound_*) or objective coefficient
    # (col_cost_*) can move before the current optimal basis changes.
    col_bound_up: dict[str, RangingRecord] = field(default_factory=dict)
    col_bound_dn: dict[str, RangingRecord] = field(default_factory=dict)
    col_cost_up: dict[str, RangingRecord] = field(default_factory=dict)
    col_cost_dn: dict[str, RangingRecord] = field(default_factory=dict)
    # Keyed by ROW (constraint) name, same convention as `duals` -- how
    # far that row's own RHS can move before the basis changes.
    row_bound_up: dict[str, RangingRecord] = field(default_factory=dict)
    row_bound_dn: dict[str, RangingRecord] = field(default_factory=dict)
    # Variable value by name (nimbus issue #490) -- x's own array indexed
    # by name, same insertion order as duals/reduced_costs/col_bound_*
    # above. Exists purely so bound_headroom() can look up "this
    # variable's own current value" without needing LPProblem's own
    # name->index map plumbed through (LPResult has no back-reference to
    # the LPProblem that produced it, by design -- see value_of()/
    # values_of() on LPProblem for the normal, problem-side way to read
    # x by name). Empty whenever x itself is None (non-optimal result).
    _x_by_name: dict[str, float] = field(default_factory=dict)
    # Same idea as `_x_by_name`, for rhs_headroom() -- each row's own
    # REAL achieved LHS value (HiGHS's `row_value`), keyed by row name,
    # same order `duals` was built from. For an equality row this always
    # equals that row's own rhs exactly; for a ub row it can sit strictly
    # below rhs whenever the constraint isn't binding, which is exactly
    # why rhs_headroom() needs the real achieved value here rather than
    # just re-reporting the raw row_bound_dn/up endpoints.
    _row_value_by_name: dict[str, float] = field(default_factory=dict)

    # nimbus issue #494 (Signals 5/7 of #489): the live highspy instance
    # this result came off of, retained ONLY when the caller opted in via
    # LPProblem.solve(keep_basis=True) -- see sweep_cost()'s own
    # docstring. None (the default, same "opt-in, no ambient cost"
    # convention as ranging_valid=None) for every ordinary solve, so a
    # ordinary caller never keeps a live C++ object alive for a
    # capability it never asked for. `compare=False`/`repr=False`: a
    # highspy.Highs instance has no meaningful equality/repr for this
    # dataclass's own purposes, and no existing caller ever compares two
    # LPResult instances for equality (confirmed via a repo-wide search
    # before adding this).
    _highs: Any = field(default=None, repr=False, compare=False)
    # Copy of the LPProblem's own name->column-index map, needed by
    # sweep_cost() to call h.changeColCost(col, ...) -- LPResult has no
    # back-reference to the LPProblem that produced it, by design (same
    # reasoning as _x_by_name's own docstring above), so this is copied
    # in at solve() time instead. Empty whenever _highs is None.
    _var_index: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    # This variable's own objective coefficient AT SOLVE TIME, one entry
    # per variable name -- sweep_cost() restores it after sweeping so a
    # caller sweeping several different variables off the SAME LPResult
    # (network.py's own import/export offer-curve pair) never sees one
    # sweep's leftover cost bleed into the next. Empty whenever _highs is
    # None.
    _orig_cost_by_name: dict[str, float] = field(
        default_factory=dict, repr=False, compare=False
    )
    # nimbus issue #676: the full ordered variable/row name lists, same
    # ones _build_ranging_dict() uses to resolve a raw HiGHS in_var_/
    # ou_var_ index back to a real name -- needed by
    # sweep_cost_with_ranging() to decode a per-step h.getRanging() call
    # the same way the original solve's own ranging block already does.
    # Copied in at solve() time for the same reason _var_index is (no
    # back-reference to the LPProblem that produced this result). Empty
    # whenever _highs is None -- only ever needed alongside a retained
    # live basis.
    _var_names: list[str] = field(default_factory=list, repr=False, compare=False)
    _row_names: list[str] = field(default_factory=list, repr=False, compare=False)

    def sweep_cost(self, var: str, costs: list[float]) -> list[float]:
        """Re-solve this LP once per entry in `costs`, resetting `var`'s
        own objective coefficient to that value each time, and return
        `var`'s own resulting value at each step (same order as
        `costs`) -- nimbus issue #494 (Signals 5/7 of #489), the
        mechanism behind the offer-curve price sweep.

        Warm-started from THIS result's own already-loaded optimal
        basis -- each step is a genuine HiGHS re-optimize from a near-
        optimal starting point, not a cold solve from scratch, which is
        the entire reason to do this here (inside the live `h`) instead
        of building N brand-new LPProblems. Only the ONE named
        variable's cost coefficient changes between steps; every bound
        and constraint is untouched, so the feasible region never
        changes -- a step can change WHICH vertex is optimal, never
        whether the problem has a feasible/bounded answer at all.

        `var`'s own cost coefficient is restored to its original
        (solve-time) value before this method returns, regardless of
        what `costs` swept through -- see `_orig_cost_by_name`'s own
        docstring for why.

        Raises ValueError if this LPResult didn't retain a live HiGHS
        instance (`solve(keep_basis=True)` wasn't passed) or if any
        sweep step doesn't come back optimal (a real anomaly worth
        surfacing loudly, not silently returning a garbage value from
        an unsolved re-optimize).
        """
        if self._highs is None:
            msg = (
                f"sweep_cost({var!r}) requires solve(keep_basis=True) -- "
                "this LPResult did not retain its own HiGHS instance"
            )
            raise ValueError(msg)
        if var not in self._var_index:
            msg = f"Unknown variable {var!r}"
            raise KeyError(msg)
        h = self._highs
        col = self._var_index[var]
        original_cost = self._orig_cost_by_name.get(var, 0.0)
        values: list[float] = []
        try:
            for cost in costs:
                h.changeColCost(col, cost)
                h.run()
                if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
                    msg = (
                        f"sweep_cost({var!r}): re-solve at cost={cost} did not "
                        f"reach optimal (status={h.modelStatusToString(h.getModelStatus())!r})"
                    )
                    raise ValueError(msg)
                values.append(float(h.getSolution().col_value[col]))
        finally:
            h.changeColCost(col, original_cost)
        return values

    def sweep_cost_with_ranging(
        self, var: str, costs: list[float]
    ) -> list[SweepRangingStep]:
        """Same warm-started re-solve loop as `sweep_cost()` (nimbus issue
        #676), additionally computing `var`'s own exact cost-ranging at
        EVERY step, not only its resulting value -- each returned
        `SweepRangingStep.cost_dn`/`cost_up` is the real, exact price
        interval that step's own `value` holds for, straight from HiGHS's
        own post-solve ranging on that exact re-solved basis, not a
        bracket inferred from neighbouring sample points.

        This is genuinely more expensive than plain `sweep_cost()` -- a
        real HiGHS ranging pass on top of every single re-solve, not
        free. A caller that only needs the sampled values (no exact-
        interval need) should keep using `sweep_cost()`; this method
        exists for callers that specifically want per-step sensitivity
        (the offer curve's own `#676`), not as a strict replacement.

        Does NOT require `solve(ranging=True)` on the ORIGINAL solve --
        `h.getRanging()` reads whatever solved state `h` currently holds
        at the moment it's called, the same way the original solve's own
        ranging block (`_solve_highs()`) calls it after `h.run()`; this
        method simply calls it again after each of ITS OWN re-solves.
        Only `solve(keep_basis=True)` is required, same as `sweep_cost()`.

        Raises the same ValueError/KeyError as `sweep_cost()` or wasn't
        passed `keep_basis=True`, or `var` doesn't exist, or a re-solve
        step doesn't reach optimal. A step where ranging itself comes
        back invalid (`rng.valid` is `False` -- a genuine edge case, not
        the common case) does NOT raise: that step's own `cost_dn`/
        `cost_up` are simply `None`, the same honest-representation
        convention `bound_headroom()` already uses, rather than treating
        a per-step ranging failure as fatal to the whole sweep.

        `var`'s own cost coefficient is restored to its original value
        before returning, identical guarantee to `sweep_cost()`.
        """
        if self._highs is None:
            msg = (
                f"sweep_cost_with_ranging({var!r}) requires solve(keep_basis=True) -- "
                "this LPResult did not retain its own HiGHS instance"
            )
            raise ValueError(msg)
        if var not in self._var_index:
            msg = f"Unknown variable {var!r}"
            raise KeyError(msg)
        h = self._highs
        col = self._var_index[var]
        var_names = self._var_names
        row_names = self._row_names
        original_cost = self._orig_cost_by_name.get(var, 0.0)
        steps: list[SweepRangingStep] = []
        try:
            for cost in costs:
                h.changeColCost(col, cost)
                h.run()
                if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
                    msg = (
                        f"sweep_cost_with_ranging({var!r}): re-solve at cost={cost} did "
                        f"not reach optimal (status={h.modelStatusToString(h.getModelStatus())!r})"
                    )
                    raise ValueError(msg)
                value = float(h.getSolution().col_value[col])
                _rng_status, rng = h.getRanging()
                if rng.valid:
                    cost_dn = RangingRecord(
                        value=float(rng.col_cost_dn.value_[col]),
                        objective=float(rng.col_cost_dn.objective_[col]),
                        in_var=_ranging_name_or_none(
                            int(rng.col_cost_dn.in_var_[col]), var_names, row_names
                        ),
                        out_var=_ranging_name_or_none(
                            int(rng.col_cost_dn.ou_var_[col]), var_names, row_names
                        ),
                    )
                    cost_up = RangingRecord(
                        value=float(rng.col_cost_up.value_[col]),
                        objective=float(rng.col_cost_up.objective_[col]),
                        in_var=_ranging_name_or_none(
                            int(rng.col_cost_up.in_var_[col]), var_names, row_names
                        ),
                        out_var=_ranging_name_or_none(
                            int(rng.col_cost_up.ou_var_[col]), var_names, row_names
                        ),
                    )
                else:
                    cost_dn = None
                    cost_up = None
                steps.append(
                    SweepRangingStep(value=value, cost_dn=cost_dn, cost_up=cost_up)
                )
        finally:
            h.changeColCost(col, original_cost)
        return steps

    def bound_headroom(self, var: str) -> Headroom | None:
        """Real (down, up) headroom on variable `var`'s own bound before
        the optimal basis changes -- `x[var] - col_bound_dn[var].value`
        and `col_bound_up[var].value - x[var]`, so a caller never has to
        re-derive this delta convention itself. None when ranging isn't
        available (not requested, or HiGHS reported it invalid) or
        `var` has no ranging entry (e.g. ranging was requested on a
        different, earlier-solved LPResult).

        See `Headroom`'s own docstring for the zero-width/`degenerate`
        convention (nimbus issue #490, following HAEO #465's own
        "saturated" concept, but reported explicitly rather than
        silently folded into a fallback)."""
        if var not in self.col_bound_up or var not in self.col_bound_dn:
            return None
        x_val = self._x_by_name[var]
        return _headroom_from(
            x_val - self.col_bound_dn[var].value,
            self.col_bound_up[var].value - x_val,
        )

    def rhs_headroom(self, row: str) -> Headroom | None:
        """Same shape as `bound_headroom()`, for a constraint row's own
        RHS instead of a variable's own bound -- `row_value[row] -
        row_bound_dn[row].value` and `row_bound_up[row].value -
        row_value[row]`, where `row_value` is that row's own REAL
        achieved LHS sum (equal to `rhs` exactly for an equality row;
        can sit strictly below `rhs` for a ub row that isn't currently
        binding)."""
        if row not in self.row_bound_up or row not in self.row_bound_dn:
            return None
        row_val = self._row_value_by_name[row]
        return _headroom_from(
            row_val - self.row_bound_dn[row].value,
            self.row_bound_up[row].value - row_val,
        )


@dataclass
class LPProblem:
    """A linear program in natural form. Build one with add_variable() /
    add_ub_constraint() / add_eq_constraint(), then call solve().

    Deliberately a thin, explicit builder rather than raw numpy arrays --
    real callers (network.py) are assembling a problem from many small
    per-element contributions across many periods, and naming variables
    by index alone (as raw scipy.optimize.linprog-style arrays require)
    is exactly the kind of silent-off-by-one risk this project's own
    history (interval-mismatch bugs, elsewhere in this codebase) warns
    against. Named variables/constraints make a wrong index a loud
    KeyError instead of a silent wrong answer.
    """

    _var_names: list[str] = field(default_factory=list)
    _var_index: dict[str, int] = field(default_factory=dict)
    _lb: list[float] = field(default_factory=list)
    _ub: list[float] = field(default_factory=list)
    _cost: dict[str, float] = field(default_factory=dict)
    # nimbus issue #696, Stage 1: the SECONDARY objective channel --
    # see this module's own top-of-file comment for the full
    # primary/secondary architecture. Only ever read when `solve()` is
    # given a real `options=` value; a plain `solve()` (options=None,
    # the default) never looks at this dict at all, so populating it on
    # a problem that never opts into `options=` is a harmless no-op,
    # not a behavior change.
    _secondary_cost: dict[str, float] = field(default_factory=dict)
    _ub_rows: list[tuple[dict[str, float], float]] = field(default_factory=list)
    _eq_rows: list[tuple[dict[str, float], float]] = field(default_factory=list)
    # Parallel name lists (2026-08-18, dual-value extraction) -- kept
    # SEPARATE from _ub_rows/_eq_rows (rather than folding name into each
    # row tuple) so every existing caller's `(terms, rhs)` unpacking
    # elsewhere in this module keeps working unchanged. Always exactly
    # len(_ub_rows)/len(_eq_rows) long, entries default to None (auto-named
    # at solve time) when a caller doesn't pass name=.
    _ub_row_names: list[str | None] = field(default_factory=list)
    _eq_row_names: list[str | None] = field(default_factory=list)
    # Per-variable integrality flag (2026-08-27, nimbus issue #238).
    # Always exactly len(_var_names) long; all-False means this problem is
    # a pure LP and _solve_highs() takes its original single-solve path
    # unchanged, byte-identical to before binaries existed.
    _binary: list[bool] = field(default_factory=list)

    @property
    def is_mip(self) -> bool:
        """True when any variable has been registered as binary. Callers use
        this to reason about solve cost -- a MIP is branch-and-bound, not a
        single simplex solve, so the timing characteristics differ.
        """
        return any(self._binary)

    def add_variable(
        self,
        name: str,
        *,
        lb: float = 0.0,
        ub: float = float("inf"),
        cost: float = 0.0,
        binary: bool = False,
    ) -> str:
        """Register a new variable. Returns `name` unchanged, so this can be
        chained inline where a variable is first used (`x = p.add_variable(...)`).
        Raises if `name` is already registered -- a silent overwrite here
        would be exactly the kind of bug this whole named-variable design
        exists to prevent.

        `binary=True` (2026-08-27, nimbus issue #238) registers the variable
        as an integer restricted to [0, 1], turning the whole problem into a
        MIP. HiGHS is a real MIP solver (this module has always been a thin
        highspy layer, see the module docstring), so this needs no separate
        backend -- but it does change the solve from one simplex run to
        branch-and-bound, and it removes meaningful duals from the MIP solve
        itself. `_solve_highs()` handles the latter by fixing every binary to
        its solved value and re-solving the resulting pure LP, so callers that
        depend on duals/reduced costs (network.py's power-balance dual, the
        single most economically meaningful number the model produces) keep
        working unchanged. Passing an explicit lb/ub alongside binary=True is
        rejected rather than silently ignored.
        """
        if name in self._var_index:
            msg = f"Variable {name!r} already registered"
            raise ValueError(msg)
        if binary:
            if lb != 0.0 or ub != float("inf"):
                msg = (
                    f"Variable {name!r}: binary=True fixes bounds to [0, 1]; "
                    f"got lb={lb}, ub={ub}. Drop the explicit bounds."
                )
                raise ValueError(msg)
            lb, ub = 0.0, 1.0
        if lb > ub:
            msg = f"Variable {name!r} has lb={lb} > ub={ub}"
            raise ValueError(msg)
        self._var_index[name] = len(self._var_names)
        self._var_names.append(name)
        self._lb.append(lb)
        self._ub.append(ub)
        self._binary.append(binary)
        if cost != 0.0:
            self._cost[name] = cost
        return name

    def set_cost(self, name: str, cost: float) -> None:
        """Add to (not replace) this variable's objective coefficient --
        multiple cost contributions (e.g. a discharge cost AND a shadow
        P2P price on the same variable) are additive, and forcing every
        caller to pre-sum them before calling this would be real, avoidable
        friction for network.py's own per-element cost assembly.
        """
        if name not in self._var_index:
            msg = f"Unknown variable {name!r}"
            raise KeyError(msg)
        self._cost[name] = self._cost.get(name, 0.0) + cost

    def set_secondary_cost(self, name: str, cost: float) -> None:
        """Same additive-not-replace semantics as `set_cost()`, on the
        SECONDARY objective channel instead (nimbus issue #696, Stage
        1) -- a tie-break PREFERENCE among primary-optimal solutions,
        never a real economic cost. Only meaningful when `solve()` is
        given a real `options=` value; a plain `solve()` never reads
        this channel at all. See this module's own top-of-file comment
        for the full primary/secondary architecture."""
        if name not in self._var_index:
            msg = f"Unknown variable {name!r}"
            raise KeyError(msg)
        self._secondary_cost[name] = self._secondary_cost.get(name, 0.0) + cost

    def add_ub_constraint(
        self, terms: dict[str, float], rhs: float, *, name: str | None = None
    ) -> None:
        """sum(coef * var for var, coef in terms) <= rhs.

        `name` (2026-08-18, optional, backward compatible) tags this row so
        LPResult.duals can report its shadow price by a meaningful key
        (e.g. "export_bonus_cap_2026-08-18") instead of an anonymous row
        index. Unnamed rows still get a dual value at solve time (auto-
        named `ub_{i}`) -- naming is purely for readability, never required
        for a row to be covered.
        """
        self._check_terms(terms)
        self._ub_rows.append((dict(terms), rhs))
        self._ub_row_names.append(name)

    def add_eq_constraint(
        self, terms: dict[str, float], rhs: float, *, name: str | None = None
    ) -> None:
        """sum(coef * var for var, coef in terms) == rhs. This is the
        mechanism every real power-balance constraint (§ network.py) uses --
        "power in equals power out at this node, this period" is always an
        equality, never a bound.

        `name`: see add_ub_constraint()'s own docstring -- identical
        purpose and same auto-naming fallback (`eq_{i}`) here.
        """
        self._check_terms(terms)
        self._eq_rows.append((dict(terms), rhs))
        self._eq_row_names.append(name)

    def _check_terms(self, terms: dict[str, float]) -> None:
        unknown = [name for name in terms if name not in self._var_index]
        if unknown:
            msg = f"Unknown variable(s) in constraint: {unknown}"
            raise KeyError(msg)

    @property
    def n_variables(self) -> int:
        return len(self._var_names)

    def solve(
        self,
        *,
        ranging: bool = False,
        keep_basis: bool = False,
        options: SolveOptions | None = None,
    ) -> LPResult:
        """`ranging=True` (nimbus issue #490) additionally computes
        HiGHS's own post-solve ranging (see `LPResult`'s own docstring
        for the full field-by-field meaning) -- opt-in, since it's a
        real extra solver pass most callers never need. On a MIP
        (`is_mip`), ranging is computed on the pinned-and-relaxed pure
        LP AFTER branch-and-bound has already chosen the integer
        assignment (the same pass `_solve_highs()` already runs to
        recover meaningful duals on a MIP, see that function's own
        comment) -- valid at the real chosen assignment, something HAEO
        (LP-only) structurally can't offer.

        `keep_basis=True` (nimbus issue #494) additionally retains the
        live highspy instance on the returned LPResult, enabling
        `LPResult.sweep_cost()` -- see that method's own docstring. Also
        opt-in, for the same reason: an ordinary caller never needs a
        live C++ object to outlive this call, and every LPResult that
        doesn't ask for one stays exactly as cheap to solve/discard as
        before this parameter existed.

        `options=` (nimbus issue #696, Stage 1) opts into the real
        primary/secondary objective architecture -- see this module's
        own top-of-file comment. `None` (the default) is BYTE-IDENTICAL
        to this module's pre-#696 behavior: a single blended solve using
        only `_cost`, `_secondary_cost` never read at all.

        On a MIP (`is_mip`), nimbus issue #702 (#696's own tracked
        follow-up) extends this: the binaries are solved ONCE against
        the primary objective alone (a plain branch-and-bound pass,
        `_secondary_cost` never involved), then pinned to that real
        chosen assignment and relaxed to continuous -- the same
        "pin-and-relax" technique `_solve_highs()` already uses to
        recover meaningful duals on a MIP, reused here for a different
        reason (so the phased/calibrated machinery below sees a genuine
        LP, not a mix of integrality and multi-phase objectives, which
        is genuinely new, unexplored territory with no HAEO precedent
        to port from). The residual pure LP then runs the exact same
        lex/blended/calibrated logic as the non-MIP path. This means
        the secondary objective's own guarantee (never overriding a
        real primary difference) extends to which CONTINUOUS variables
        get tie-broken once the binary assignment is fixed, but never
        second-guesses the binary assignment itself -- that was already
        decided by the primary-only MIP solve before any secondary cost
        is read.
        """
        return _solve_highs(
            self, ranging=ranging, keep_basis=keep_basis, options=options
        )

    def value_of(self, result: LPResult, name: str) -> float:
        """Read one named variable's value out of a solved LPResult.
        The public counterpart to indexing result.x directly by raw
        position -- callers (network.py) should never need to know that
        LPProblem keeps its own name->index mapping internally.
        """
        if result.x is None:
            msg = f"Cannot read variable values from a non-optimal result (status={result.status!r})"
            raise ValueError(msg)
        return float(result.x[self._var_index[name]])

    def values_of(self, result: LPResult, names: list[str]) -> NDArray[np.float64]:
        """Vectorized counterpart to value_of() -- the common case in
        network.py is reading a whole period-indexed array of one
        variable kind at once (e.g. every battery_charge_{t}).
        """
        if result.x is None:
            msg = f"Cannot read variable values from a non-optimal result (status={result.status!r})"
            raise ValueError(msg)
        return np.array([result.x[self._var_index[name]] for name in names])


def _ranging_name_or_none(
    idx: int, var_names: list[str], row_names: list[str]
) -> str | None:
    """Maps one HiGHS ranging `in_var_`/`ou_var_` index back to a real
    name (nimbus issue #490). `-1` is HiGHS's own "no variable" sentinel
    (a problem's own outermost breakpoint can genuinely have nothing
    entering/leaving the basis there). Confirmed directly against a live
    highspy install: an index `>= len(var_names)` is NOT out of range --
    HiGHS indexes the combined [structural columns][row slacks] basis
    space here, so index `len(var_names) + k` names the k-th ROW's own
    slack, not a structural variable at all. Reported as that row's own
    name (the natural reading: "this constraint's own slack entered/left
    the basis"), never a raw index or a crash."""
    if idx < 0:
        return None
    if idx < len(var_names):
        return var_names[idx]
    row_idx = idx - len(var_names)
    if row_idx < len(row_names):
        return row_names[row_idx]
    # Defensive only -- every index HiGHS has ever returned in testing
    # fell into one of the two cases above; never silently mis-attribute
    # an unrecognized index to the wrong name if this assumption is ever
    # wrong on some future HiGHS version.
    return None


def _build_ranging_dict(
    keys: list[str],
    rec: Any,
    var_names: list[str],
    row_names: list[str],
    *,
    limit: int | None = None,
) -> dict[str, RangingRecord]:
    # `limit` (nimbus issue #490): highspy's own col_cost_up/col_cost_dn
    # ranging records come back ONE ELEMENT LONGER than the real number
    # of columns (confirmed directly against a live highspy install,
    # every other ranging record -- col_bound_*, row_bound_* -- is
    # exactly the expected length) -- a real, undocumented quirk of the
    # binding, not a modeling choice of this project's own. The trailing
    # extra entry doesn't correspond to any real variable, so callers
    # for col_cost_up/dn pass `limit=len(var_names)` to drop it rather
    # than risk a `keys[i]` IndexError or, worse, a silently wrong
    # off-by-one variable attribution.
    n_use = limit if limit is not None else len(keys)
    return {
        keys[i]: RangingRecord(
            value=float(rec.value_[i]),
            objective=float(rec.objective_[i]),
            in_var=_ranging_name_or_none(int(rec.in_var_[i]), var_names, row_names),
            out_var=_ranging_name_or_none(int(rec.ou_var_[i]), var_names, row_names),
        )
        for i in range(n_use)
    }


def _dense_cost_vector(
    problem: LPProblem, cost: dict[str, float]
) -> NDArray[np.float64]:
    """nimbus issue #696: build a dense per-column cost vector from a
    name-keyed cost dict, in the same var_array/column order
    `_solve_highs()` itself uses -- the array form single-call
    objective switching (`h.changeColsCost`) needs. Adapted from HAEO's
    own `network.py::_build_cost_vectors()`, which does the equivalent
    starting from a `highs_linear_expression` rather than a plain dict."""
    vec = np.zeros(problem.n_variables, dtype=np.float64)
    for i, name in enumerate(problem._var_names):
        vec[i] = cost.get(name, 0.0)
    return vec


def _set_cost_vector(
    h: highspy.Highs, col_indices: NDArray[np.int32], costs: NDArray[np.float64]
) -> None:
    """Set the full objective cost vector in a single C call --
    `col_indices` covers ALL variables, so every column's cost is
    replaced; no stale cost from a previous phase can persist. Verbatim
    technique from HAEO's own `network.py::_set_cost_vector()`."""
    h.changeColsCost(len(col_indices), col_indices, costs)
    h.changeObjectiveOffset(0.0)


def _ensure_optimal_value(h: highspy.Highs) -> float:
    """Run the solver and return its objective value, raising if the
    result isn't optimal. An intermediate lex/calibration PHASE failing
    to reach optimal is a genuine anomaly here (every phase operates on
    the exact same feasible region the caller's own variables/
    constraints already established) -- unlike the CALLER's own final
    result, where "infeasible"/"error" are normal, expected `LPResult`
    outcomes (see that class's own docstring)."""
    h.run()
    status = h.getModelStatus()
    if status != highspy.HighsModelStatus.kOptimal:
        msg = (
            "LPProblem.solve(options=...): an internal lex/calibration "
            f"phase failed to reach optimal (status={h.modelStatusToString(status)!r})"
        )
        raise ValueError(msg)
    return float(h.getObjectiveValue())


def _pin_binaries_to_current_solution(
    h: highspy.Highs, var_array: list[Any], binary_cols: list[int]
) -> None:
    """Pin every binary column to its CURRENT solved value (whatever
    solve last ran against `h`) and relax it to continuous. Same
    "solve, pin, relax" technique `_solve_highs()` already uses to
    recover meaningful duals on a MIP (see that function's own "Recover
    duals on a MIP" comment) -- factored out here so `_solve_with_
    options()` can call it at the RIGHT point in its own phase sequence
    (see nimbus issue #702's real design finding below), not before any
    phase has run.

    Mutates `h` in place -- returns nothing, matching
    `_solve_with_options()`'s own "mutate the live solver state"
    convention. No-op when `binary_cols` is empty (a pure LP)."""
    if not binary_cols:
        return
    x = np.array([h.val(var_array[i]) for i in range(len(var_array))])
    for i in binary_cols:
        fixed = float(round(x[i]))
        h.changeColIntegrality(i, highspy.HighsVarType.kContinuous)
        h.changeColBounds(i, fixed, fixed)


def _bisect_boundary(
    lo: float,
    hi: float,
    predicate: Callable[[float], bool],
    *,
    max_steps: int,
    convergence: float,
) -> float:
    """Binary search for the boundary where `predicate` flips True ->
    False. Assumes `predicate(lo)` is True and `predicate(hi)` is
    False. Returns the highest value where predicate still holds.
    Verbatim technique from HAEO's own `network.py::_bisect_boundary()`."""
    for _ in range(max_steps):
        if hi - lo < convergence:
            break
        mid = (lo + hi) / 2
        if predicate(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _calibrate_blend_weight(
    h: highspy.Highs,
    col_indices: NDArray[np.int32],
    primary_vec: NDArray[np.float64],
    secondary_vec: NDArray[np.float64],
    lex_primary_cost: float,
    tolerance: float,
) -> float:
    """Find the largest blend weight whose single-solve primary cost
    stays within `tolerance` (relative) of the true lex-optimal primary
    cost, then run one final blended solve at that weight -- so `h`'s
    own live state (duals/reduced costs/ranging read off it afterward)
    reflects a genuine single blended solve, the same guarantee HAEO's
    own implementation gives itself. Adapted directly from that
    project's own `network.py::_calibrate_blend_weight()` -- same log10
    bisection, same one-sided "primary cost must not exceed the lex
    optimum by more than tolerance" acceptance criterion, same
    `_CAL_MARGIN` step-back for robustness against coefficient drift."""
    if lex_primary_cost == 0.0 and not np.any(primary_vec):
        weight = 1e-3  # safe default -- no primary cost to distort
        blended = primary_vec + weight * secondary_vec
        _set_cost_vector(h, col_indices, blended)
        h.run()
        return weight

    abs_tol = max(1e-8, abs(lex_primary_cost) * tolerance)

    def _primary_acceptable(log_w: float) -> bool:
        w = 10.0**log_w
        blended = primary_vec + w * secondary_vec
        _set_cost_vector(h, col_indices, blended)
        h.run()
        if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            return False
        bl_vals = np.asarray(h.getSolution().col_value)
        bl_primary_cost = float(primary_vec @ bl_vals)
        return bl_primary_cost <= lex_primary_cost + abs_tol

    lo, hi = _CAL_LOG_LO, _CAL_LOG_HI
    if _primary_acceptable(hi):
        upper = hi
    elif _primary_acceptable(lo):
        upper = _bisect_boundary(
            lo,
            hi,
            _primary_acceptable,
            max_steps=_CAL_MAX_STEPS,
            convergence=_CAL_CONVERGENCE,
        )
    else:
        _LOGGER.warning(
            "LPProblem.solve(options=CalibratedOptions(...)): no blend weight "
            "preserves primary cost within tolerance (%.2e); using minimum weight %.2e",
            abs_tol,
            10.0**lo,
        )
        upper = lo

    weight_log = max(lo, upper - _CAL_MARGIN)
    weight = 10.0**weight_log

    blended = primary_vec + weight * secondary_vec
    _set_cost_vector(h, col_indices, blended)
    h.run()
    return weight


def _solve_with_options(
    h: highspy.Highs,
    var_array: list[Any],
    col_indices: NDArray[np.int32],
    problem: LPProblem,
    options: SolveOptions,
    binary_cols: list[int],
) -> list[str]:
    """Runs the real phased/blended/calibrated solve against an ALREADY
    fully-constructed HiGHS model (every variable/constraint already
    added by `_solve_highs()`) -- mutates `h`'s own live state so the
    existing post-solve reading code (duals, reduced costs, ranging,
    keep_basis) in `_solve_highs()` works completely unchanged
    regardless of which mode ran. Direct, adapted port of HAEO's own
    `network.py::optimize()`/`_solve_lex()`/`_solve_blended()` dispatch
    -- see this module's own top-of-file comment for the full
    architecture and the deliberate scope cuts versus that source.

    Returns the names of any EXTRA constraint rows this function added
    to `h` beyond the caller's own -- unlike HAEO (which has no
    equivalent concern), `_solve_highs()` builds a `row_names` list
    that must stay in exact 1:1 order with `h`'s own rows for its
    `duals` dict extraction (`zip(row_names, solution.row_dual,
    strict=True)`) -- a lex-phase constraint row that isn't accounted
    for there would silently break that zip. Empty for `BlendedOptions`
    (adds no rows at all).

    Deliberately does NOT call `h.clearLinearObjectives()` the way
    HAEO's own `_solve_lex()`/`_solve_blended()` do at their start --
    that exists there to clear a PERSISTENT solver instance's prior
    objective between repeated calls over a `Network`'s lifetime.
    Nimbus's own `_solve_highs()` builds a brand-new `highspy.Highs()`
    for every single call (no persistent reuse), so there is no prior
    objective state to clear, and `_set_cost_vector()`'s own
    `changeColsCost` call already overwrites every column each time
    it's used regardless.

    `binary_cols` (nimbus issue #702): when non-empty, EVERY solve this
    function performs up to and including the phase that fixes the real
    binary assignment (see below) keeps integrality ACTIVE -- a genuine
    branch-and-bound pass, not a pre-pinned LP. This was a real, wrong
    first design tried for #702: pinning binaries to a PRIMARY-ONLY MIP
    solve's own result BEFORE any secondary cost is read seemed safe
    (never overrides a real primary difference), but empirically
    neuters any tie-break whose only expression is via which binaries
    get chosen -- confirmed directly on a real adequacy-load scenario
    where multiple binary assignments tie on true primary cost:
    `adequacy_earliness_budget_kw`'s own secondary cost, which should
    prefer the EARLIEST tied assignment, had no continuous variable
    left free to express that preference once the binaries were already
    locked in by a solve that never looked at it -- the result landed
    on an ARBITRARY (in this case the LATEST) tied assignment instead,
    a real regression versus this project's own pre-#702 MIP fallback
    behavior (which bakes the same preference directly into primary and
    reliably prefers early). The fix: keep integrality active through
    phase 2 as well (secondary, hard-constrained not to worsen primary)
    -- a real second branch-and-bound pass, letting the binary
    assignment move freely among every PRIMARY-tied integer solution to
    find the one that also minimizes secondary. Only once that real
    tie-break has happened is the (now correctly chosen) binary
    assignment pinned and relaxed, via `_pin_binaries_to_current_
    solution()`, so any further phase (Lex's epsilon-restore, or
    Calibrated's blend-weight search) sees a genuine LP -- same
    reasoning as before, just moved to the right point in the sequence.
    `BlendedOptions` needs no separate phase for this: a single MIP
    solve on the blended objective directly already lets the binary
    respond to secondary cost the same way a continuous variable would,
    since there's no separate hard-constrained phase to sequence around
    at all -- pinning still happens immediately after, purely so `h`'s
    own live state carries genuine LP duals rather than raw branch-and-
    bound output.

    Two full branch-and-bound passes (plus whatever Lex/Calibrated need
    afterward) is real, understood extra solve cost versus the single
    MIP solve `options=None` performs -- accepted for now given every
    real MIP this project solves today is small (a handful of adequacy
    loads' own on/off + start binaries, see #616), well within
    `DEFAULT_TIME_LIMIT_SECONDS`'s per-call budget; worth revisiting if
    a much larger real MIP ever appears in practice."""
    primary_vec = _dense_cost_vector(problem, problem._cost)
    secondary_vec = _dense_cost_vector(problem, problem._secondary_cost)

    if isinstance(options, BlendedOptions):
        blended = primary_vec + options.blend_weight * secondary_vec
        _set_cost_vector(h, col_indices, blended)
        h.run()
        _pin_binaries_to_current_solution(h, var_array, binary_cols)
        if binary_cols:
            # Refresh h's own live state on the now-continuous, pinned
            # problem -- same point as _ensure_optimal_value() would
            # make, but a blended-cost solve's result is never checked
            # against any acceptance criterion the way Lex/Calibrated's
            # own phases are, so a plain re-run is enough here.
            h.run()
        return []

    # LexOptions and CalibratedOptions both start with the same phase 1
    # + phase 2: minimize primary alone, then minimize secondary with
    # primary HARD-constrained not to get worse than its own phase-1
    # optimum -- the real guarantee this whole architecture exists for
    # (secondary can never override a real price signal, not even by an
    # epsilon). Both phases keep integrality active when binary_cols is
    # non-empty (see this function's own docstring) -- phase 2 is a
    # real second branch-and-bound pass specifically so the binary
    # assignment itself can respond to secondary cost among every
    # primary-tied integer solution, not just whichever one phase 1
    # happened to find first.
    _set_cost_vector(h, col_indices, primary_vec)
    primary_value = _ensure_optimal_value(h)

    # Same "always >= 1 term" dense-iteration style as this module's own
    # pre-#696 cost_expr construction (every variable, missing/zero
    # entries included) -- guarantees a non-empty qsum regardless of how
    # many primary/secondary costs are actually nonzero.
    primary_expr = highspy.Highs.qsum(
        float(coef) * var_array[i] for i, coef in enumerate(primary_vec)
    )
    h.addConstr(primary_expr <= primary_value)
    extra_row_names = ["_lex_primary_le_optimum"]
    _set_cost_vector(h, col_indices, secondary_vec)
    secondary_value = _ensure_optimal_value(h)

    # nimbus issue #702: the real binary assignment is now decided --
    # phase 2's own (possibly MIP) solve just chose, among every
    # PRIMARY-tied integer solution, the one that also minimizes
    # secondary. Pin it now so every phase from here on (Lex's epsilon
    # restore, or Calibrated's blend-weight search) operates on a
    # genuine LP.
    _pin_binaries_to_current_solution(h, var_array, binary_cols)
    if binary_cols:
        # Re-solve (now a pure LP) so h's own live state -- read by the
        # phases below, and by _solve_highs()'s own duals/ranging
        # extraction afterward -- reflects a genuine LP result at this
        # exact point, not raw branch-and-bound internals. Pinning
        # fixed every binary to the value it already held, so this
        # reproduces the identical solution; the point is a clean solve
        # record, not a different answer.
        _set_cost_vector(h, col_indices, secondary_vec)
        _ensure_optimal_value(h)

    if isinstance(options, LexOptions):
        # Phase 3: re-minimize primary with a tiny relative epsilon
        # slack on secondary -- restores clean primary duals/reduced
        # costs without perturbing the phase-2 point. HAEO's own
        # reasoning, ported verbatim.
        epsilon = max(1e-6, abs(secondary_value) * 1e-6)
        secondary_expr = highspy.Highs.qsum(
            float(coef) * var_array[i] for i, coef in enumerate(secondary_vec)
        )
        h.addConstr(secondary_expr <= secondary_value + epsilon)
        extra_row_names.append("_lex_secondary_le_optimum")
        _set_cost_vector(h, col_indices, primary_vec)
        _ensure_optimal_value(h)
        return extra_row_names

    # CalibratedOptions: h's own live basis already sits at the phase-2
    # (true lex) optimum -- read it off directly rather than re-solving,
    # then search for a safe blend weight and do one final blended solve.
    lex_values = np.asarray(h.getSolution().col_value)
    lex_primary_cost = float(primary_vec @ lex_values)
    _calibrate_blend_weight(
        h,
        col_indices,
        primary_vec,
        secondary_vec,
        lex_primary_cost,
        options.calibration_tolerance,
    )
    return extra_row_names


def _solve_highs(
    problem: LPProblem,
    *,
    ranging: bool = False,
    keep_basis: bool = False,
    options: SolveOptions | None = None,
) -> LPResult:
    """Translate an LPProblem into a highspy model, solve it, and translate
    the result back. See this module's own docstring for why highspy
    (not a from-scratch simplex) is the real solver backend here.

    `ranging` (nimbus issue #490): see LPProblem.solve()'s own docstring.
    `keep_basis` (nimbus issue #494): see LPProblem.solve()'s own docstring.
    `options` (nimbus issue #696): see LPProblem.solve()'s own docstring.
    """
    n = problem.n_variables
    if n == 0:
        return LPResult(status="optimal", x=np.zeros(0), objective=0.0, iterations=0)

    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    # nimbus issue #356 (Mark Purcell): no time limit was ever set, so a
    # genuinely pathological problem (e.g. a MIP -- see issue #238's own
    # binary-variable groundwork -- that branch-and-bound can't close
    # quickly) could block indefinitely. This runs on HA's own shared
    # executor thread pool (solver_writer.py's `hass.async_add_executor_
    # job()`), a limited, shared resource -- an unbounded solve there
    # doesn't just delay one solve cycle, it can starve whatever else HA
    # is trying to run on that same pool. Every real solve this project
    # has ever measured completes in well under a second even at full
    # production scale (~4000 variables, ~1500 constraints, per this
    # module's own docstring) -- 60s is generous headroom for a
    # legitimately large problem, while still bounding a genuinely stuck
    # solve to a small fraction of even the fastest solve cadence this
    # project runs (the native runtime's own 1-minute timer).
    h.setOptionValue("time_limit", DEFAULT_TIME_LIMIT_SECONDS)

    # Real, confirmed live (2026-08-18): highspy's own batched
    # addVariables(n, lb=array, ub=array) form REJECTS per-variable array
    # bounds ("Invalid parameter") -- only a single shared scalar bound
    # across all n variables works there (exactly HAEO's own usage
    # pattern -- node.py's `solver.addVariables(n, lb=0, ...)`, where
    # every one of those n variables genuinely does share the same
    # bound). Nimbus's own variables have real per-variable bounds
    # (different max_charge_kw per battery, a real nonzero lb on SoC,
    # etc.), so this adds one at a time instead -- confirmed via a real
    # timing test at production scale (~4000 variables) that this loop
    # costs ~0.05s, nowhere near a real bottleneck at this problem size.
    var_array: list[Any] = []
    for i in range(n):
        lb = problem._lb[i]
        ub = problem._ub[i]
        highs_lb = -highspy.kHighsInf if lb == float("-inf") else lb
        highs_ub = highspy.kHighsInf if ub == float("inf") else ub
        var_array.append(h.addVariable(lb=highs_lb, ub=highs_ub))

    # Integrality (2026-08-27, nimbus issue #238). Applied AFTER every
    # column exists so the column indices here match insertion order
    # exactly -- same ordering guarantee row_names below relies on.
    # problem._binary is all-False for every pre-existing caller, so this
    # loop is a no-op and the solve below stays the identical single
    # simplex run it has always been.
    binary_cols = [i for i in range(n) if problem._binary[i]]
    for i in binary_cols:
        h.changeColIntegrality(i, highspy.HighsVarType.kInteger)

    # Row names, built in the EXACT same order rows are added below (ub
    # rows first, then eq rows) -- this order is what h.getSolution().
    # row_dual is indexed by, so it must match precisely or a dual value
    # would silently get attributed to the wrong constraint. Auto-named
    # per-list (ub_0, ub_1, ... / eq_0, eq_1, ...) rather than one global
    # counter, so names stay stable/predictable regardless of how many of
    # each kind exist.
    row_names: list[str] = [
        given or f"ub_{i}" for i, given in enumerate(problem._ub_row_names)
    ] + [given or f"eq_{i}" for i, given in enumerate(problem._eq_row_names)]

    for terms, rhs in problem._ub_rows:
        expr = highspy.Highs.qsum(
            coef * var_array[problem._var_index[name]] for name, coef in terms.items()
        )
        h.addConstr(expr <= rhs)
    for terms, rhs in problem._eq_rows:
        expr = highspy.Highs.qsum(
            coef * var_array[problem._var_index[name]] for name, coef in terms.items()
        )
        h.addConstr(expr == rhs)

    extra_row_names: list[str] = []
    if options is None:
        # Dense-style cost expression (every variable, defaulting missing
        # entries to 0.0) -- matches the old from-scratch solver's own
        # `phase2_cost = np.zeros(...)` convention, and guarantees the qsum
        # generator always has n >= 1 terms (n == 0 already returned above),
        # never an empty one, regardless of whether ANY set_cost() call was
        # ever actually made for this particular problem. Byte-identical to
        # this module's pre-#696 behavior -- `_secondary_cost` is never
        # even read on this path.
        cost_expr = highspy.Highs.qsum(
            problem._cost.get(name, 0.0) * var_array[i]
            for i, name in enumerate(problem._var_names)
        )
        h.minimize(cost_expr)
    else:
        col_indices = np.arange(n, dtype=np.int32)
        # nimbus issue #702 (#696's own tracked MIP follow-up):
        # binary_cols passed straight through -- _solve_with_options()
        # itself now handles a MIP's own binary pinning at the RIGHT
        # point in its phase sequence (see that function's own
        # docstring for the real design finding that drove this). Empty
        # for every non-MIP caller, so this stays a zero-cost pass-
        # through on the pre-#702 path.
        extra_row_names = _solve_with_options(
            h, var_array, col_indices, problem, options, binary_cols
        )

    # nimbus issue #696: a lex/calibrated phase adds real extra
    # constraint ROWS to `h` beyond the caller's own (see
    # `_solve_with_options()`'s own docstring) -- appended here, in the
    # same order they were added to `h`, so `row_names` stays in exact
    # 1:1 order with `h`'s own rows for the duals extraction below.
    # Empty for options=None and for BlendedOptions, so this is a
    # no-op on every pre-#696 path.
    row_names = row_names + extra_row_names

    # nimbus issue #490: on any non-optimal status below, ranging_valid
    # is False (not None) whenever ranging was actually requested -- the
    # caller asked, and the honest answer is "no, this model isn't in a
    # state ranging is meaningful for". None (the default) is reserved
    # for "never asked".
    _ranging_valid_on_non_optimal = False if ranging else None

    status = h.getModelStatus()
    iterations = int(h.getInfo().simplex_iteration_count)
    if status == highspy.HighsModelStatus.kInfeasible:
        return LPResult(
            status="infeasible",
            iterations=iterations,
            ranging_valid=_ranging_valid_on_non_optimal,
        )
    if status == highspy.HighsModelStatus.kUnbounded:
        return LPResult(
            status="unbounded",
            iterations=iterations,
            ranging_valid=_ranging_valid_on_non_optimal,
        )
    if status != highspy.HighsModelStatus.kOptimal:
        # nimbus issue #356 (Mark Purcell): every other non-optimal status
        # (kTimeLimit, kIterationLimit, kSolutionLimit, kUnknown,
        # kUnboundedOrInfeasible, kModelError, kSolveError) used to be
        # surfaced as "infeasible" too -- indistinguishable from a model
        # HiGHS actually proved has no feasible dispatch at all. None of
        # these are that: they're all genuine SOLVER-level failures (gave
        # up, hit a limit, hit an internal error) on a model whose real
        # feasibility was never actually determined either way. Reported
        # as "error" instead, with HiGHS's own status name preserved in
        # raw_status so a caller/log line can name the real cause.
        return LPResult(
            status="error",
            iterations=iterations,
            raw_status=h.modelStatusToString(status),
            ranging_valid=_ranging_valid_on_non_optimal,
        )

    x = np.array([h.val(var_array[i]) for i in range(n)])
    objective = float(h.getObjectiveValue())

    # Recover duals on a MIP (2026-08-27, nimbus issue #238). A MIP has no
    # meaningful dual solution -- branch-and-bound doesn't produce one, and
    # HiGHS returns zeros/garbage in row_dual rather than raising. Every
    # dual consumer downstream (network.py's power_balance_t{t} dual, the
    # reduced costs the quality report reads) would silently get wrong
    # numbers rather than an error, which is exactly the failure class this
    # module's named-variable design exists to avoid.
    #
    # Standard fix: once branch-and-bound has chosen the integer
    # assignment, that assignment IS the answer -- pinning each binary to
    # its solved value and relaxing integrality leaves a pure LP whose
    # optimum is the same point, and whose duals are the real marginal
    # prices of the constraints AT that assignment. Values are rounded
    # before pinning because HiGHS returns integers within its own
    # tolerance (0.9999999) rather than exactly.
    #
    # nimbus issue #702: on the options-not-None MIP path, this pinning
    # already happened -- BEFORE _solve_with_options() ran, via
    # _pin_binaries_to_mip_optimum() -- so every solve since then
    # (including the phased/calibrated machinery's own final one) was
    # already against the pinned, continuous problem, and `h`'s current
    # live state already carries real LP duals. Redoing this block would
    # be harmless (bounds are already fixed to the same values) but a
    # wasted extra solve every MIP+options cycle -- skipped here rather
    # than silently re-running it.
    if binary_cols and options is None:
        for i in binary_cols:
            fixed = float(round(x[i]))
            h.changeColIntegrality(i, highspy.HighsVarType.kContinuous)
            h.changeColBounds(i, fixed, fixed)
        h.run()
        if h.getModelStatus() == highspy.HighsModelStatus.kOptimal:
            # Re-read from the pinned LP: x is unchanged by construction
            # (every binary pinned, every continuous variable re-optimised
            # against the same constraints), but the objective and the
            # solution struct below now carry real LP duals.
            x = np.array([h.val(var_array[i]) for i in range(n)])
            objective = float(h.getObjectiveValue())

    # Dual values (row_dual) and reduced costs (col_dual), 2026-08-18 --
    # both come off the same HighsSolution struct, indexed by row/column
    # insertion order respectively. row_dual's ORDER must match row_names
    # built above exactly (ub rows then eq rows, in the order each list
    # was appended) -- both are built from problem._ub_rows/_eq_rows in
    # that same fixed order, so this is safe by construction, not by
    # coincidence.
    solution = h.getSolution()
    duals = dict(zip(row_names, solution.row_dual, strict=True))
    reduced_costs = dict(zip(problem._var_names, solution.col_dual, strict=True))

    # nimbus issue #490 (Signals 1/7 of #489): ranging computed here, on
    # the SAME `h` the duals/reduced_costs above just came off of -- for
    # a MIP that means AFTER the pin-and-relax re-solve above, valid at
    # the real chosen integer assignment, never the original branch-and-
    # bound tree. `rng.valid` is HiGHS's own honest answer to "is this
    # actually usable" (False on the rare pin-and-relax-itself-non-
    # optimal edge case too, with no special-casing needed here) --
    # trusted directly rather than re-derived from `status` a second
    # time.
    ranging_valid: bool | None = None
    col_bound_up: dict[str, RangingRecord] = {}
    col_bound_dn: dict[str, RangingRecord] = {}
    col_cost_up: dict[str, RangingRecord] = {}
    col_cost_dn: dict[str, RangingRecord] = {}
    row_bound_up: dict[str, RangingRecord] = {}
    row_bound_dn: dict[str, RangingRecord] = {}
    x_by_name: dict[str, float] = {}
    row_value_by_name: dict[str, float] = {}
    if ranging:
        _log_start = time.monotonic()
        _rng_status, rng = h.getRanging()
        ranging_valid = bool(rng.valid)
        if ranging_valid:
            col_bound_up = _build_ranging_dict(
                problem._var_names, rng.col_bound_up, problem._var_names, row_names
            )
            col_bound_dn = _build_ranging_dict(
                problem._var_names, rng.col_bound_dn, problem._var_names, row_names
            )
            col_cost_up = _build_ranging_dict(
                problem._var_names,
                rng.col_cost_up,
                problem._var_names,
                row_names,
                limit=n,
            )
            col_cost_dn = _build_ranging_dict(
                problem._var_names,
                rng.col_cost_dn,
                problem._var_names,
                row_names,
                limit=n,
            )
            row_bound_up = _build_ranging_dict(
                row_names, rng.row_bound_up, problem._var_names, row_names
            )
            row_bound_dn = _build_ranging_dict(
                row_names, rng.row_bound_dn, problem._var_names, row_names
            )
            x_by_name = dict(zip(problem._var_names, x, strict=True))
            row_value_by_name = dict(zip(row_names, solution.row_value, strict=True))
        # nimbus issue #490's own "Cost" section: log the ranging pass's
        # own time at DEBUG, next to the existing iteration count -- the
        # same visibility HAEO #465 gives itself for this same real,
        # measurable extra solver cost.
        _LOGGER.debug(
            "Nimbus lp.py: ranging pass took %.4fs (valid=%s)",
            time.monotonic() - _log_start,
            ranging_valid,
        )

    # nimbus issue #494: captured unconditionally (cheap -- a dict
    # comprehension over n names) so it's ready the moment keep_basis
    # wants it, but only actually STORED on the result (via the
    # conditional below) when keep_basis=True -- an ordinary solve pays
    # for building this dict but not for retaining `h` itself, which is
    # the real, measured cost this parameter exists to gate.
    orig_cost_by_name = {
        name: problem._cost.get(name, 0.0) for name in problem._var_names
    }

    return LPResult(
        status="optimal",
        x=x,
        objective=objective,
        iterations=iterations,
        ranging_valid=ranging_valid,
        col_bound_up=col_bound_up,
        col_bound_dn=col_bound_dn,
        col_cost_up=col_cost_up,
        col_cost_dn=col_cost_dn,
        row_bound_up=row_bound_up,
        row_bound_dn=row_bound_dn,
        _x_by_name=x_by_name,
        _row_value_by_name=row_value_by_name,
        duals=duals,
        reduced_costs=reduced_costs,
        _highs=h if keep_basis else None,
        _var_index=dict(problem._var_index) if keep_basis else {},
        _orig_cost_by_name=orig_cost_by_name if keep_basis else {},
        # nimbus issue #676: needed by sweep_cost_with_ranging() to decode
        # a per-step h.getRanging() call the same way this function's own
        # ranging block above does -- same keep_basis-gated, only-pay-for-
        # it-if-retained convention as _var_index/_orig_cost_by_name.
        _var_names=list(problem._var_names) if keep_basis else [],
        _row_names=list(row_names) if keep_basis else [],
    )
