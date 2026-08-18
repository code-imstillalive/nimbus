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


@dataclass(frozen=True)
class LPResult:
    """Outcome of solve(). `status` is always one of "optimal", "infeasible",
    or "unbounded" -- never an exception for a genuinely infeasible/unbounded
    problem, since both are real, expected outcomes a caller (network.py)
    needs to handle explicitly, not treat as a crash. `x`/`objective` are
    only meaningful when status == "optimal".
    """

    status: str
    x: NDArray[np.float64] | None = None
    objective: float | None = None
    iterations: int = 0


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

    def add_variable(self, name: str, *, lb: float = 0.0, ub: float = float("inf"), cost: float = 0.0) -> str:
        """Register a new variable. Returns `name` unchanged, so this can be
        chained inline where a variable is first used (`x = p.add_variable(...)`).
        Raises if `name` is already registered -- a silent overwrite here
        would be exactly the kind of bug this whole named-variable design
        exists to prevent.
        """
        if name in self._var_index:
            msg = f"Variable {name!r} already registered"
            raise ValueError(msg)
        if lb > ub:
            msg = f"Variable {name!r} has lb={lb} > ub={ub}"
            raise ValueError(msg)
        self._var_index[name] = len(self._var_names)
        self._var_names.append(name)
        self._lb.append(lb)
        self._ub.append(ub)
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
            raise KeyError(name)
        self._cost[name] = self._cost.get(name, 0.0) + cost

    def add_ub_constraint(self, terms: dict[str, float], rhs: float) -> None:
        """sum(coef * var for var, coef in terms) <= rhs."""
        self._check_terms(terms)
        self._ub_rows.append((dict(terms), rhs))

    def add_eq_constraint(self, terms: dict[str, float], rhs: float) -> None:
        """sum(coef * var for var, coef in terms) == rhs. This is the
        mechanism every real power-balance constraint (§ network.py) uses --
        "power in equals power out at this node, this period" is always an
        equality, never a bound.
        """
        self._check_terms(terms)
        self._eq_rows.append((dict(terms), rhs))

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

    for terms, rhs in problem._ub_rows:
        expr = highspy.Highs.qsum(coef * var_array[problem._var_index[name]] for name, coef in terms.items())
        h.addConstr(expr <= rhs)
    for terms, rhs in problem._eq_rows:
        expr = highspy.Highs.qsum(coef * var_array[problem._var_index[name]] for name, coef in terms.items())
        h.addConstr(expr == rhs)

    # Dense-style cost expression (every variable, defaulting missing
    # entries to 0.0) -- matches the old from-scratch solver's own
    # `phase2_cost = np.zeros(...)` convention, and guarantees the qsum
    # generator always has n >= 1 terms (n == 0 already returned above),
    # never an empty one, regardless of whether ANY set_cost() call was
    # ever actually made for this particular problem.
    cost_expr = highspy.Highs.qsum(
        problem._cost.get(name, 0.0) * var_array[i] for i, name in enumerate(problem._var_names)
    )
    h.minimize(cost_expr)

    status = h.getModelStatus()
    iterations = int(h.getInfo().simplex_iteration_count)
    if status == highspy.HighsModelStatus.kInfeasible:
        return LPResult(status="infeasible", iterations=iterations)
    if status == highspy.HighsModelStatus.kUnbounded:
        return LPResult(status="unbounded", iterations=iterations)
    if status != highspy.HighsModelStatus.kOptimal:
        # Any other non-optimal status (e.g. a solver-level issue short
        # of a clean infeasible/unbounded classification) is surfaced as
        # infeasible rather than silently mishandled -- matches this
        # module's own long-standing "status is always optimal,
        # infeasible, or unbounded" contract, never a fourth case a
        # caller (network.py) hasn't been told to expect.
        return LPResult(status="infeasible", iterations=iterations)

    x = np.array([h.val(var_array[i]) for i in range(n)])
    objective = float(h.getObjectiveValue())
    return LPResult(status="optimal", x=x, objective=objective, iterations=iterations)
