"""A small, general-purpose linear program solver -- pure numpy, no
scipy/PuLP/highspy. Same reasoning as ml/gbrt.py's own from-scratch GBRT:
this integration has no C compiler and no confirmed wheel availability
for a compiled LP library inside the HA container it deploys into (HAEO's
own equivalent dependency is `highspy`, a compiled package) -- rather than
gate the whole solver on an unverified dependency, this implements the
standard two-phase revised-simplex-on-a-dense-tableau method directly.

This is intentionally NOT trying to be a general-purpose, industrial LP
solver. The real Nimbus Solver's own problems are small (a handful of
elements) and re-solved frequently, not huge one-off problems -- a dense
tableau is the right tradeoff here: simple, easy to verify correct, fast
enough at this scale. If a future version needs a genuinely large
problem, that's the point to revisit this decision, not before.

Real horizon size, corrected 2026-08-18 (the original "tens to low
hundreds of periods" estimate above predates the 96h tiered horizon --
see nimbus_solver_forecast_writer.py's own TIER0/TIER1/TIER2 constants):
production solves run to ~365 periods (5 x 1-min + 288 x 5-min + 72 x
1-hour), plus proximal-regularization variables when a previous_plan is
given -- several thousand variables/constraints once expanded to
standard form. See _STALL_THRESHOLD's own comment for what this meant
for the pivot rule below.

Problem form accepted (the "natural" LP form, not standard form -- this
module handles the standard-form conversion internally so callers never
have to think about slack/artificial variables):

    minimize    c^T x
    subject to  A_ub x <= b_ub      (any number of rows, may be empty)
                A_eq x  = b_eq      (any number of rows, may be empty)
                lb <= x <= ub       (per-variable, -inf/+inf allowed)

All real Nimbus Solver LP problems (see network.py) are built this way:
free variables (e.g. battery power, which can be positive or negative)
get lb=-inf, ub=+inf and are handled internally via variable splitting
(x = x+ - x-, both >= 0) -- the standard, well-understood technique for
handling free variables in a nonnegative-variable simplex formulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

# Numerical tolerances. 1e-9 is tight enough to distinguish real zero from
# real nonzero at the kW/hour scale this solver operates at (typical
# coefficients are O(1) to O(100), never anywhere near float64's own
# precision floor), loose enough to not misfire on ordinary floating-point
# noise from repeated pivot operations.
_EPS: float = 1e-9
# Safety valve against a genuine implementation bug (a cycling or
# never-terminating pivot sequence) turning into a silent infinite loop.
# Raised 10_000 -> 50_000 (2026-08-18) alongside the Dantzig/Bland hybrid
# pivot rule below -- see _STALL_THRESHOLD's own comment for the real
# incident this responds to. With Dantzig doing the bulk of the real
# work, 50_000 is a genuine safety margin against a real implementation
# bug, not a number expected to be approached in practice.
_MAX_ITERATIONS: int = 50_000

# How many CONSECUTIVE degenerate (zero-length) pivots to tolerate under
# the fast Dantzig rule before permanently switching to Bland's rule for
# the rest of THIS solve. Real incident (2026-08-18): the real Nimbus
# Solver horizon (see nimbus_solver_forecast_writer.py's own 96h tiered
# grid -- ~5 minutes of 1-min resolution + 24h of 5-min resolution + 72h
# of 1-hour resolution, ~365 periods) plus proximal-regularization
# variables (previous_plan stability, see network.py) produces a real LP
# with several thousand variables/constraints -- confirmed locally via a
# standalone repro matching the real production build_plan() call shape:
# a PURE Bland's-rule solve on this scale did not converge even after
# several minutes of real wall-clock time, let alone within the original
# 10_000-iteration cap. This is NOT the cycling Bland's rule exists to
# prevent -- it's Bland's rule's own well-known, real cost (lowest-index
# pivot selection ignores how much each candidate actually improves the
# objective, so it can take vastly more pivots than a value-aware rule
# on anything beyond a small textbook-sized problem) colliding with a
# genuinely larger-than-originally-assumed real problem size (the module
# docstring's own "tens to low hundreds of periods" assumption, written
# before the 96h tiered horizon existed).
#
# Fix: Dantzig's rule (most-negative reduced cost -- the standard,
# far-fewer-pivots-in-practice default) drives every pivot UNTIL a real
# stall is detected, then this solver permanently switches to Bland's
# rule for the remainder of THIS solve. This is safe, not a compromise:
# cycling can ONLY occur through a run of purely degenerate (zero
# objective improvement) pivots -- a pivot that strictly improves the
# objective can never revisit a basis already seen, since the objective
# changes monotonically along any such sequence. Tracking consecutive
# degenerate pivots therefore detects real cycling RISK directly, and
# Bland's rule's own anti-cycling proof holds from whatever basis it's
# switched on at -- it does not depend on how the solve got there.
_STALL_THRESHOLD: int = 200


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
        return _solve_two_phase(self)

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


def _solve_two_phase(problem: LPProblem) -> LPResult:
    """Two-phase simplex on a dense tableau, with Bland's rule (lowest-index
    pivot selection, both for entering and leaving variables on ties) used
    throughout -- Bland's rule is the standard, well-known guarantee against
    cycling in degenerate LPs, at some cost in pivot count vs. a more
    aggressive pivot rule. Given this solver's own real problem sizes (see
    module docstring), correctness/simplicity is the right tradeoff over
    raw pivot-count performance.

    Free variables (lb=-inf) are split into (positive part) - (negative
    part), both constrained >= 0 -- the standard technique. A finite-but-
    nonzero lower bound is handled by substituting x' = x - lb (shifting
    the variable so its own effective lower bound becomes exactly 0) and
    undoing the shift on the returned solution. A finite upper bound
    becomes an extra <= row.
    """
    n_orig = problem.n_variables
    if n_orig == 0:
        return LPResult(status="optimal", x=np.zeros(0), objective=0.0, iterations=0)

    # ---- Build the working variable list: each original variable becomes
    # either one shifted nonnegative variable (finite lb) or two
    # nonnegative variables (free, lb=-inf) whose difference recovers the
    # original value. `parts[i]` records how to reconstruct original
    # variable i from the working-variable solution.
    parts: list[tuple[int, int | None, float]] = []  # (pos_idx, neg_idx_or_None, shift)
    work_names: list[str] = []
    work_cost: list[float] = []
    extra_ub_rows: list[tuple[dict[str, float], float]] = []  # for finite ub

    for i, name in enumerate(problem._var_names):
        lb = problem._lb[i]
        ub = problem._ub[i]
        cost = problem._cost.get(name, 0.0)
        if lb == float("-inf"):
            pos_name = f"{name}__pos"
            neg_name = f"{name}__neg"
            pos_idx = len(work_names)
            work_names.append(pos_name)
            work_cost.append(cost)
            neg_idx = len(work_names)
            work_names.append(neg_name)
            work_cost.append(-cost)
            parts.append((pos_idx, neg_idx, 0.0))
            if ub != float("inf"):
                extra_ub_rows.append(({name: 1.0}, ub))
        else:
            shifted_name = f"{name}__shift"
            pos_idx = len(work_names)
            work_names.append(shifted_name)
            work_cost.append(cost)
            parts.append((pos_idx, None, lb))
            if ub != float("inf"):
                # Real bug, found 2026-08-16 while testing rolling.py
                # against a battery scenario with a large min_soc_kwh
                # (this project's SoC variable is the only one anywhere
                # in this package with a nonzero lb -- every other
                # variable has lb=0.0). This row is later run through
                # _expand_row() below, which ITSELF subtracts `shift`
                # (== lb) via `shift_adjustment` to convert from
                # original-variable terms to working-variable terms.
                # Passing `ub - lb` here (already shifted once) then
                # gets shifted a SECOND time by _expand_row, silently
                # capping the variable's real usable ceiling at
                # `ub - 2*lb` instead of `ub - lb` (i.e., in original
                # terms, capping it at `ub - lb` instead of the correct
                # `ub`). Invisible for any lb=0 variable (0 subtracted
                # twice is still 0) -- which is every variable in this
                # codebase except battery SoC, so this went undetected
                # through this project's entire prior test history.
                # Correct: pass the ORIGINAL, unshifted `ub` here and let
                # _expand_row's own shift_adjustment do the (single)
                # subtraction, exactly like every other row in this
                # solver already does.
                extra_ub_rows.append(({name: 1.0}, ub))

    def _expand_row(terms: dict[str, float]) -> dict[str, float]:
        """Rewrite a constraint given in ORIGINAL variable names into one
        given in WORKING variable names, applying the split/shift above.
        A finite-lb variable's shift contributes a constant to the RHS
        side (handled by the caller, since this returns only the
        coefficient dict), tracked via the returned shift-adjustment.
        """
        out: dict[str, float] = {}
        shift_adjustment = 0.0
        for name, coef in terms.items():
            i = problem._var_index[name]
            pos_idx, neg_idx, shift = parts[i]
            out[work_names[pos_idx]] = out.get(work_names[pos_idx], 0.0) + coef
            if neg_idx is not None:
                out[work_names[neg_idx]] = out.get(work_names[neg_idx], 0.0) - coef
            elif shift != 0.0:
                shift_adjustment += coef * shift
        return out, shift_adjustment  # noqa: RET504 (clarity over micro-optimization here)

    n_work = len(work_names)
    ub_rows: list[tuple[dict[str, float], float]] = []
    for terms, rhs in problem._ub_rows:
        expanded, adj = _expand_row(terms)
        ub_rows.append((expanded, rhs - adj))
    for terms, rhs in extra_ub_rows:
        expanded, adj = _expand_row(terms)
        ub_rows.append((expanded, rhs - adj))
    eq_rows: list[tuple[dict[str, float], float]] = []
    for terms, rhs in problem._eq_rows:
        expanded, adj = _expand_row(terms)
        eq_rows.append((expanded, rhs - adj))

    n_slack = len(ub_rows)
    n_total_before_artificial = n_work + n_slack
    m = len(ub_rows) + len(eq_rows)

    # Build A (m x n_total_before_artificial), b (m,), tracking which rows
    # need an artificial variable (any <= row with a negative RHS after
    # negation into >= form, and every = row) to seed a feasible basis for
    # Phase 1.
    a_matrix = np.zeros((m, n_total_before_artificial))
    b_vec = np.zeros(m)
    name_to_col = {name: idx for idx, name in enumerate(work_names)}
    needs_artificial = np.zeros(m, dtype=bool)

    row = 0
    for terms, rhs in ub_rows:
        for name, coef in terms.items():
            a_matrix[row, name_to_col[name]] = coef
        slack_col = n_work + row
        a_matrix[row, slack_col] = 1.0
        if rhs < 0:
            a_matrix[row, :] *= -1.0
            rhs = -rhs
            needs_artificial[row] = True
        b_vec[row] = rhs
        row += 1
    for terms, rhs in eq_rows:
        for name, coef in terms.items():
            a_matrix[row, name_to_col[name]] = coef
        if rhs < 0:
            a_matrix[row, :] *= -1.0
            rhs = -rhs
        b_vec[row] = rhs
        needs_artificial[row] = True
        row += 1

    artificial_rows = np.where(needs_artificial)[0]
    n_artificial = len(artificial_rows)
    if n_artificial > 0:
        artificial_block = np.zeros((m, n_artificial))
        for k, r in enumerate(artificial_rows):
            artificial_block[r, k] = 1.0
        a_full = np.hstack([a_matrix, artificial_block])
    else:
        a_full = a_matrix

    n_total = n_total_before_artificial + n_artificial
    basis = np.empty(m, dtype=np.int64)
    art_cursor = 0
    for r in range(m):
        if needs_artificial[r]:
            basis[r] = n_total_before_artificial + art_cursor
            art_cursor += 1
        else:
            basis[r] = n_work + r  # the slack column for this row

    if n_artificial > 0:
        phase1_cost = np.zeros(n_total)
        phase1_cost[n_total_before_artificial:] = 1.0
        status, tableau, basis, iters1 = _simplex_core(a_full, b_vec, phase1_cost, basis)
        if status != "optimal":
            # Phase 1 itself being unbounded can't happen for a valid
            # artificial-cost problem (it's bounded below by 0) -- if this
            # ever triggers, it's a real implementation bug, not a
            # legitimate LP outcome, so it's surfaced as infeasible rather
            # than silently mishandled.
            return LPResult(status="infeasible", iterations=iters1)
        # The tableau's own bottom-right cell stores -(objective) by the
        # standard simplex convention set up in _simplex_core (initialized
        # there as -(c_b @ b), maintained through every pivot) -- NEGATE
        # it back here. Real bug, caught by test_lp_correctness.py's
        # dedicated infeasible-via-constraints case: every earlier test
        # happened to have a true Phase-1 objective of exactly 0 (a
        # genuinely feasible problem), where the sign doesn't matter, so
        # this went undetected until a problem with a real, nonzero
        # Phase-1 objective (i.e. genuine infeasibility) was tested --
        # the un-negated read silently inverted the infeasibility check
        # and let the solver return a wrong "optimal" result instead.
        phase1_obj = -float(tableau[-1, -1])
        if phase1_obj > 1e-7:
            return LPResult(status="infeasible", iterations=iters1)
        # Drive any artificial variable still in the basis (at value ~0,
        # a genuine degenerate case) out before Phase 2, so Phase 2 never
        # has to reason about artificial columns at all.
        for r in range(m):
            if basis[r] >= n_total_before_artificial:
                pivoted_out = False
                for c in range(n_total_before_artificial):
                    if abs(tableau[r, c]) > _EPS:
                        tableau = _pivot(tableau, r, c)
                        basis[r] = c
                        pivoted_out = True
                        break
                if not pivoted_out:
                    # This row is a genuinely redundant constraint (all
                    # zero coefficients on every real variable) -- drop it
                    # from further consideration by fixing it at 0 cost
                    # contribution; the row itself is left in the tableau
                    # but can never re-enter the basis meaningfully.
                    pass
        working_a = tableau[:m, :n_total_before_artificial]
        working_b = tableau[:m, -1]
    else:
        working_a = a_full[:m, :n_total_before_artificial]
        working_b = b_vec
        iters1 = 0

    phase2_cost = np.zeros(n_total_before_artificial)
    for name, cost_val in zip(work_names, work_cost, strict=True):
        phase2_cost[name_to_col[name]] = cost_val

    status, tableau2, basis2, iters2 = _simplex_core(working_a, working_b, phase2_cost, basis)
    total_iters = iters1 + iters2
    if status == "unbounded":
        return LPResult(status="unbounded", iterations=total_iters)
    if status != "optimal":
        return LPResult(status="infeasible", iterations=total_iters)

    work_solution = np.zeros(n_total_before_artificial)
    for r in range(m):
        if basis2[r] < n_total_before_artificial:
            work_solution[basis2[r]] = tableau2[r, -1]

    x_orig = np.empty(n_orig)
    for i in range(n_orig):
        pos_idx, neg_idx, shift = parts[i]
        val = work_solution[pos_idx]
        if neg_idx is not None:
            val -= work_solution[neg_idx]
        else:
            val += shift
        x_orig[i] = val

    objective = float(sum(problem._cost.get(name, 0.0) * x_orig[i] for i, name in enumerate(problem._var_names)))
    return LPResult(status="optimal", x=x_orig, objective=objective, iterations=total_iters)


def _simplex_core(
    a_matrix: NDArray[np.float64],
    b_vec: NDArray[np.float64],
    cost: NDArray[np.float64],
    basis: NDArray[np.int64],
) -> tuple[str, NDArray[np.float64], NDArray[np.int64], int]:
    """Run simplex to optimality (or detect unboundedness) starting from an
    already-feasible basis. Returns (status, final_tableau, final_basis,
    iterations). `status` is "optimal" or "unbounded" only -- infeasibility
    is a Phase-1 concept, decided by the caller from the Phase-1 objective
    value, not detected here.

    Hybrid Dantzig/Bland pivot rule (2026-08-18) -- see _STALL_THRESHOLD's
    own comment for the full real-incident reasoning. Dantzig's rule
    (most-negative reduced cost) drives every pivot by default; the
    moment _STALL_THRESHOLD consecutive pivots have all been degenerate
    (zero real progress -- the ONLY way a simplex can ever cycle), this
    permanently switches to Bland's rule (lowest-index selection, both
    entering and on leaving-row ties) for the rest of this call, which is
    provably safe from cycling from that point forward regardless of the
    pivot rule used before the switch.
    """
    m, n = a_matrix.shape
    tableau = np.zeros((m + 1, n + 1))
    tableau[:m, :n] = a_matrix
    tableau[:m, -1] = b_vec
    basis = basis.copy()

    # Reduced-cost row: cost - c_B^T @ tableau (standard simplex setup,
    # expressing the objective purely in terms of nonbasic variables).
    c_b = cost[basis]
    tableau[-1, :n] = cost - c_b @ tableau[:m, :n]
    tableau[-1, -1] = -(c_b @ tableau[:m, -1])

    iterations = 0
    bland_mode = False
    stall_count = 0
    while iterations < _MAX_ITERATIONS:
        reduced = tableau[-1, :n]
        candidates = np.where(reduced < -_EPS)[0]
        if candidates.size == 0:
            return "optimal", tableau, basis, iterations

        if bland_mode:
            entering = int(candidates[0])
        else:
            entering = int(candidates[np.argmin(reduced[candidates])])

        col = tableau[:m, entering]
        positive_rows = np.where(col > _EPS)[0]
        if positive_rows.size == 0:
            return "unbounded", tableau, basis, iterations

        # Minimum-ratio test. Tie-break depends on pivot mode: Bland's
        # rule (lowest basic-variable INDEX leaves) once stalled, or
        # simply the first tied row while still in the fast Dantzig
        # phase -- correctness doesn't depend on which tied row leaves,
        # only termination-under-degeneracy does, and that's exactly
        # what the mode switch below is watching for.
        ratios = tableau[positive_rows, -1] / col[positive_rows]
        min_ratio = ratios.min()
        tied = positive_rows[np.isclose(ratios, min_ratio, atol=_EPS)]
        leaving_row = int(tied[np.argmin(basis[tied])]) if bland_mode else int(tied[0])

        # A pivot with min_ratio ~ 0 makes zero real progress (the
        # entering variable's own value stays at 0) -- this is the ONLY
        # way a simplex sequence can ever revisit a prior basis, since
        # any pivot with min_ratio > 0 strictly improves the objective
        # and can therefore never repeat. Track consecutive occurrences;
        # once a real stall is confirmed, switch to Bland's rule
        # (guaranteed no-repeat from here on) for the rest of this solve.
        if not bland_mode:
            stall_count = stall_count + 1 if min_ratio <= _EPS else 0
            if stall_count >= _STALL_THRESHOLD:
                bland_mode = True

        tableau = _pivot(tableau, leaving_row, entering)
        basis[leaving_row] = entering
        iterations += 1

    msg = f"Simplex did not converge within {_MAX_ITERATIONS} iterations -- likely a real implementation bug"
    raise RuntimeError(msg)


def _pivot(tableau: NDArray[np.float64], row: int, col: int) -> NDArray[np.float64]:
    """Standard Gauss-Jordan pivot: normalize the pivot row, eliminate the
    pivot column from every other row (including the objective row).

    Mutates `tableau` IN PLACE and returns the same array -- real
    performance fix, 2026-08-18, found while diagnosing the same real
    incident _STALL_THRESHOLD's own comment describes. Every call site
    immediately reassigns `tableau = _pivot(tableau, ...)` and never
    reads the pre-pivot array again, so the previous copy-on-every-call
    was pure, unnecessary overhead -- at the real Nimbus Solver's now-
    confirmed problem scale (several thousand columns, thousands of
    pivots per solve), reallocating and copying the FULL tableau on
    every single pivot was a real, significant cost independent of the
    pivot-COUNT fix above. Also vectorized: the elimination step below
    replaces a Python-level loop over every row with a single numpy
    broadcast (`np.outer`), removing per-row call overhead that also
    scales with tableau size.
    """
    pivot_val = tableau[row, col]
    tableau[row, :] /= pivot_val
    factors = tableau[:, col].copy()
    factors[row] = 0.0  # never eliminate the pivot row against itself
    tableau -= np.outer(factors, tableau[row, :])
    return tableau
