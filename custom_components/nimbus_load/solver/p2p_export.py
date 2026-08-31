"""Shared, reusable P2P export-commitment mechanism -- extracted VERBATIM
from network.py's own real, live-tested implementation, deliberately as a
SEPARATE module rather than a network.py refactor (direct household
constraint: network.py is real-money-adjacent production code, re-solved
every 5 minutes on NUC1/NUC2; nothing here may touch it, even to share
code, since any accidental behavior change there carries real production
risk this feature does not need to take on).

Built specifically to let solver/stochastic.py (Track A2) genuinely reason
about a P2P export commitment (a fixed, pre-committed nightly rate, see
GridConfig.fixed_export_kw's own docstring in elements.py) the same way
build_plan() already does -- both WITH and WITHOUT a P2P plan configured,
per direct household instruction: "it should be smart to know how to
balance it with p2p in play as well as without it there at all... there
will be a variety of users... different plans different suppliers... the
integration must handle and allow for variables and various scenarios."
Every function here is a complete no-op whenever the relevant GridConfig
field is None -- the exact same "off by default, on only when configured"
convention every other optional mechanism in this codebase already uses.

## Deployment scope -- devhub only, fully reversible

This module has zero live callers in production. stochastic.py itself
(the only thing that imports this module) has zero callers in
nimbus_solver_forecast_writer.py or any other NUC1/NUC2-deployed script
(confirmed by grep before this was built). The only way this code can
ever run against real dispatch decisions is a caller EXPLICITLY choosing
to import and call build_stochastic_plan() with a real P2P-configured
GridConfig -- see 116KAT-HA-AI's own scripts/
nimbus_stochastic_comparison_writer.py for the one real caller that
exists, deliberately built to run ONLY against devhub's own HA instance
(hardcoded HA_BASE, never the NUC1/NUC2 VIP), writing a shadow-mode
comparison sensor only, never touching any real dispatch entity.
Disabling this feature is a single action: stop or remove that one
script's own cron entry -- network.py, build_plan(), and every real
NUC1/NUC2 automation stay completely untouched regardless.

## What's replicated here, and why each piece is needed together

Three real mechanisms, all from network.py's own live-tested logic
(2026-08-20 onward -- see that module's own docstring for the full real
household finding behind each):

1. **Hard charge gate** (`charging_ub_during_fixed_window`) -- a period
   under a fixed export commitment must NEVER also charge, regardless of
   how attractive a downstream incentive (terminal value, a cheap import
   price) makes it look on paper. Real live incident, 2026-08-22: without
   this, the LP found a case where charging during a committed-export
   window looked profitable on paper (net_cost going positive) purely
   because charging was never taken off the table, not because it was
   ever a real economically rational trade.
2. **Export bounds pinning** (`grid_export_bounds`) -- forces that
   period's own grid_export[t] variable to EXACTLY the committed rate
   (both lb and ub), rather than leaving it as a free decision the LP
   could re-optimize away from mid-window whenever a momentarily higher
   price elsewhere looks tempting. Real, live household finding,
   2026-08-20: P2P is a matching arrangement, not a price-taking market --
   a CONSISTENT, pre-committed delivery rate is itself part of what earns
   the rate. Chasing the momentary best price doesn't execute a smarter
   version of the deal, it breaks it.
3. **Two-tier export bonus** (`add_export_bonus_variable`,
   `add_export_bonus_le_export_constraint`, `set_export_bonus_cost`,
   `add_export_bonus_cumulative_caps`) -- the first ~N kWh of real export
   each real calendar day earns close to the true achieved P2P rate,
   anything beyond that reverts to plain spot. Capped PER REAL CALENDAR
   DAY (a real bug, found and fixed 2026-08-17 in network.py, replicated
   here identically: a single global cap across a multi-day horizon lets
   the LP front-load the entire bonus into the first night it sees and
   starve every later night). Includes the same LATEST-preferred tie-
   breaker (2026-08-20, direct household correction: "our window closes
   0.00 not 23.50... period") for when export_bonus_price is near-flat
   within a day and the LP would otherwise pick an arbitrary, scattered
   ON/OFF pattern with no real economic meaning.

All three interact: (1) and (2) only ever matter for periods where
fixed_export_kw is actually set (GridConfig.fixed_export_kw is None ->
neither function does anything); (3) is a genuinely separate,
independently optional mechanism (a household can have
export_bonus_price/volume configured without ever using fixed_export_kw,
or vice versa -- see test_solver_stochastic_p2p.py's own scenario matrix,
which exercises all four real on/off combinations explicitly, not just
the "both on" case).

## Why this doesn't also need the wash-trade / combined-direction caps

network.py's own SAME-PERIOD WASH-TRADE PREVENTION and combined-direction
constraints (see that module's own docstring) are NOT part of this
module -- stochastic.py already independently replicates the two
wash-trade constraints itself (see stochastic.py's own
_add_period_vars_and_constraints, "Same-period wash-trade prevention...
both required, replicated here identically"), and neither wash-trade
constraint changes shape or needs new logic when fixed_export_kw/
export_bonus are layered on top of it -- they operate on grid_export[t]/
charge[t]/discharge[t] exactly the same way regardless of whether those
variables happen to be pinned or bonus-eligible this period. Only the
P2P-specific mechanisms above are new enough, and interdependent enough
(the hard gate, the pinning, and the per-day-grouped cap+tie-breaker all
reference each other's state), to warrant a real shared module rather
than being hand-copied a second time the way the simpler wash-trade
constraints already were.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .elements import GridConfig, PeriodGrid
from .lp import LPProblem

# Same constant, same reasoning, as network.py's own _TIE_BREAK_EPSILON --
# deliberately NOT re-derived independently. See that module's own comment
# for the full "can only ever break a genuine tie, never override a real
# price difference" guarantee this size is chosen to preserve.
TIE_BREAK_EPSILON: float = 1e-7


def charging_ub_during_fixed_window(
    t: int, grid: GridConfig, max_charge_kw: float
) -> float:
    """The real upper bound for battery_charge[t]'s own LP variable --
    0.0 (a hard, mathematically-impossible-to-choose gate, not merely
    costed against) whenever period t falls under a real fixed export
    commitment, else the normal max_charge_kw ceiling. Verbatim logic
    from network.py's own charge[] variable construction."""
    if grid.fixed_export_kw is not None and not np.isnan(grid.fixed_export_kw[t]):
        return 0.0
    return max_charge_kw


def grid_export_bounds(
    t: int, grid: GridConfig, export_limit_kw: float
) -> tuple[float, float]:
    """The real (lb, ub) pair for grid_export[t]'s own LP variable -- both
    pinned to the exact committed rate whenever period t falls under a
    real fixed export commitment, else the normal [0, export_limit_kw]
    bounds. Verbatim logic from network.py's own grid_export[] variable
    construction."""
    if grid.fixed_export_kw is not None and not np.isnan(grid.fixed_export_kw[t]):
        pinned = float(grid.fixed_export_kw[t])
        return pinned, pinned
    return 0.0, export_limit_kw


def has_export_bonus(grid: GridConfig) -> bool:
    """Whether the two-tier export bonus mechanism is configured at all --
    both export_bonus_price and export_bonus_volume_kwh must be given
    together (GridConfig's own __post_init__ already enforces this), so
    checking either is sufficient."""
    return (
        grid.export_bonus_price is not None
        and grid.export_bonus_volume_kwh is not None
    )


def add_export_bonus_variable(p: LPProblem, name: str, export_limit_kw: float) -> str:
    """Create one export_bonus LP variable for a given period -- bounded
    by export_limit_kw at construction (the same physical ceiling as
    grid_export itself). The real "can't exceed this period's own actual
    export" constraint is added separately, once grid_export[t]'s own
    variable name is known -- see add_export_bonus_le_export_constraint."""
    return p.add_variable(name, lb=0.0, ub=export_limit_kw)


def add_export_bonus_le_export_constraint(
    p: LPProblem, export_bonus_var: str, grid_export_var: str
) -> None:
    """export_bonus[t] <= grid_export[t] -- can't claim bonus volume for
    export that didn't actually happen this period. Verbatim from
    network.py's own constraint (4)."""
    p.add_ub_constraint({export_bonus_var: 1.0, grid_export_var: -1.0}, 0.0)


def set_export_bonus_cost(
    p: LPProblem,
    export_bonus_var: str,
    t: int,
    grid: GridConfig,
    hours: NDArray[np.float64],
    *,
    weight: float = 1.0,
) -> None:
    """The extra revenue credit export_bonus[t] earns, on top of whatever
    grid_export[t] already earns at the base export_price[t] rate.
    Verbatim from network.py's own cost-term loop, extended with an
    optional `weight` (default 1.0, a no-op -- network.py's own single-
    scenario build_plan() never needs anything else) so a multi-scenario
    caller like stochastic.py can scale each scenario's own bonus revenue
    by its real probability weight, exactly like every other cost term in
    that scenario already is."""
    p.set_cost(export_bonus_var, -weight * float(grid.export_bonus_price[t]) * hours[t])


def add_export_bonus_cumulative_caps(
    p: LPProblem,
    export_bonus_vars: dict[int, str],
    periods: PeriodGrid,
    grid: GridConfig,
    *,
    label: str = "",
) -> None:
    """The per-real-calendar-day cumulative cap + latest-preferred
    tie-breaker, verbatim from network.py's own post-loop block (see that
    module's own docstring, "TWO-TIER EXPORT BONUS" and the tie-breaker's
    own extensive comment, for the full "why per-day not global" and "why
    latest not earliest" reasoning -- both real, live household findings,
    not design choices made in the abstract).

    `export_bonus_vars`: a dict mapping REAL, ABSOLUTE period index (i.e.
    matching `periods.period_starts`' own indexing) to that period's own
    export_bonus LP variable name. Deliberately a dict, not a list indexed
    from 0, so a caller solving only a SUBSET of periods.n_periods (e.g.
    stochastic.py's own stage-2 range, which starts partway through the
    full horizon) can pass exactly the periods it actually built
    variables for, while `periods.period_starts[t]` still resolves each
    one to its own real calendar date correctly.

    `label`: an optional suffix folded into each constraint's own name
    (e.g. a scenario index) -- needed for READABILITY only, not LP
    correctness (lp.py's own add_ub_constraint() docstring is explicit
    that a constraint's `name` is "purely for readability, never
    required" -- every call always appends a genuinely independent row
    regardless of name collisions, confirmed directly against lp.py's own
    source and by a live mutation test before this was shipped, not
    assumed). Without a distinct `label` per scenario, two scenarios'
    own per-day cap rows would collide on the SAME name (e.g. both named
    "export_bonus_cap_2026-08-20"), which would silently make one
    scenario's own real shadow price unreachable in `LPResult.duals`
    (whichever name is looked up last wins) even though both rows are
    still fully, independently enforced in the actual solve -- this is
    the one real, if minor, consequence `label` guards against, not a
    correctness risk to the dispatch decision itself. Empty string (the
    default) is a plain, unmodified name, matching build_plan() itself
    when only ever called once per real solve.

    Falls back to ONE global constraint (network.py's own pre-2026-08-17
    behaviour) when `periods.start is None` -- no way to know where a
    real day boundary falls without real timestamps, matching
    build_plan()'s own honest fallback exactly. No-op when
    `export_bonus_vars` is empty (nothing to cap).
    """
    if not export_bonus_vars:
        return
    starts = periods.period_starts
    if starts is None:
        ordered = sorted(export_bonus_vars)
        terms = {export_bonus_vars[t]: periods.hours[t] for t in ordered}
        p.add_ub_constraint(
            terms,
            float(grid.export_bonus_volume_kwh),
            name=f"export_bonus_cap_global{label}",
        )
        for rank, t in enumerate(ordered):
            p.set_cost(export_bonus_vars[t], -TIE_BREAK_EPSILON * (rank + 1))
        return

    by_day: dict[object, list[int]] = {}
    for t in export_bonus_vars:
        by_day.setdefault(starts[t].date(), []).append(t)

    for day_date, day_indices in by_day.items():
        day_indices_sorted = sorted(day_indices)
        terms = {export_bonus_vars[t]: periods.hours[t] for t in day_indices_sorted}
        p.add_ub_constraint(
            terms,
            float(grid.export_bonus_volume_kwh),
            name=f"export_bonus_cap_{day_date.isoformat()}{label}",
        )
        # LATEST-preferred (see this module's own docstring) -- rank+1
        # grows with real chronological order within the day, so the
        # LAST real period of the day gets the most negative (most
        # preferred) cost.
        for rank, t in enumerate(day_indices_sorted):
            p.set_cost(export_bonus_vars[t], -TIE_BREAK_EPSILON * (rank + 1))
