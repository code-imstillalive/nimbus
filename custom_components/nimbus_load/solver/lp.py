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

from dataclasses import dataclass, field
from typing import Any

import highspy
import numpy as np
from numpy.typing import NDArray

# nimbus issue #356: bounds a genuinely stuck solve (see _solve_highs's own
# comment at its call site for the full reasoning) -- not a performance
# tuning knob, a safety backstop.
DEFAULT_TIME_LIMIT_SECONDS: float = 60.0


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

    def solve(self) -> LPResult:
        return _solve_highs(self)

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


def _solve_highs(problem: LPProblem) -> LPResult:
    """Translate an LPProblem into a highspy model, solve it, and translate
    the result back. See this module's own docstring for why highspy
    (not a from-scratch simplex) is the real solver backend here.
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

    # Dense-style cost expression (every variable, defaulting missing
    # entries to 0.0) -- matches the old from-scratch solver's own
    # `phase2_cost = np.zeros(...)` convention, and guarantees the qsum
    # generator always has n >= 1 terms (n == 0 already returned above),
    # never an empty one, regardless of whether ANY set_cost() call was
    # ever actually made for this particular problem.
    cost_expr = highspy.Highs.qsum(
        problem._cost.get(name, 0.0) * var_array[i]
        for i, name in enumerate(problem._var_names)
    )
    h.minimize(cost_expr)

    status = h.getModelStatus()
    iterations = int(h.getInfo().simplex_iteration_count)
    if status == highspy.HighsModelStatus.kInfeasible:
        return LPResult(status="infeasible", iterations=iterations)
    if status == highspy.HighsModelStatus.kUnbounded:
        return LPResult(status="unbounded", iterations=iterations)
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
    if binary_cols:
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

    return LPResult(
        status="optimal",
        x=x,
        objective=objective,
        iterations=iterations,
        duals=duals,
        reduced_costs=reduced_costs,
    )
