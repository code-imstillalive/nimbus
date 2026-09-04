"""Assembles a Nimbus Solver LP from element configs, solves it, and
extracts a Plan.

Deliberately, this module NEVER writes anything anywhere -- no Modbus, no
HA entity, no automation trigger. build_plan() is a pure function: real
forecast/price inputs in, a Plan dataclass out. This is the "we will not
automate it to control anything... just to see how it behaves" boundary
the whole solver stage is currently operating inside -- enforced by this
module simply never importing anything HA-related, not by a runtime
check.

See the architecture sketch's own §2 for the three-layer design (Daily
Plan / Rolling Refinement / Safety Envelope) this module is the shared
core solve mechanism for -- this file implements ONE solve, callable at
whatever cadence/horizon a caller wants; layering is a caller-level
concern, not something build_plan() itself knows about.

## Stability mechanisms (2026-08-16, extended 2026-08-20)

A bare, single-solve LP is correct but not yet STABLE across repeated
re-solves -- the user's own explicit concern: "i do not want mistakes...
i do not want dumb algorithm - i want it to be clever and responsive but
smart wise naturally adaptive not chaotic." A rolling re-solve (Layer 2,
not yet built) will call build_plan() repeatedly on a shifting horizon;
without anything below, two near-tied optimal solutions from consecutive
solves are free to flip arbitrarily -- this is the exact same shape as
HAEO's own real, documented "flash"/replan spike behaviour (see the
sibling 116KAT-HA-AI repo's own CLAUDE.md), and this project exists
specifically to not repeat that.

All three mechanisms are OFF by default (every new parameter defaults to
`None`/`0.0`/no-op) -- calling build_plan() with none of them given is
byte-for-byte the same single, bare LP solve as before this section
existed. Every existing caller (every test predating this section)
continues to work completely unchanged.

**1. Plan stability / proximal regularization** (`previous_plan`,
`proximal_weight`) -- a SOFT cost. For every period the new solve shares
a real, aligned wall-clock start time with the previous solve's own plan
(see PeriodGrid.period_starts's own docstring for why this alignment has
to be by real time, not array index), a small L1 penalty
(`proximal_weight` $/kWh-equivalent) is added on how far each of the 4
real dispatch variables (battery charge/discharge, grid import/export)
deviates from what the previous solve planned for that SAME real moment.
This is expressed as the standard LP linearization of an absolute-value
penalty (a linear solver has no native |x| term): two nonnegative
"deviation" variables per (family, period), `dev_pos - dev_neg = new -
prev`, both costed at `proximal_weight`, so the LP is only ever charged
for whichever direction the deviation actually goes. When two solutions
are genuinely economically tied, this tips the LP toward the one closer
to the previous plan instead of an arbitrary vertex of the tie -- a real
structural fix for flip-flopping, not a heuristic patch on top of one.
Deliberately small relative to real economic signals (see
`DEFAULT_PROXIMAL_WEIGHT_KW`'s own docstring): a genuine price/cost
difference should always still win.

**2. Rate limiting** (`max_rate_kw`) -- a HARD cap, not a cost. Two
distinct applications of the same mechanism:
  - Cross-solve (period 0 only): if period 0 of the new grid aligns by
    real time to a period in `previous_plan`, each of the 4 dispatch
    variables is hard-bounded within `[prev_value - max_rate_kw,
    prev_value + max_rate_kw]` -- this is what actually protects the
    real inverter from being commanded to swing from e.g. -40kW to +40kW
    between two consecutive dispatch cycles, independent of whatever the
    LP would otherwise prefer. Requires `previous_plan`; silently skipped
    (not an error) if no aligned period-0 previous value exists (e.g.
    the very first solve ever).
  - Intra-plan (every consecutive pair within the new horizon): the SAME
    cap also bounds every period t's dispatch variables relative to
    period t-1's, for the whole new plan -- this needs no previous_plan
    at all (t-1 is itself a variable in the same LP), and directly
    protects against a chaotic-looking plan that swings hard between
    adjacent future periods, not just at the moment of dispatch.
  `max_rate_kw=None` (default) disables both -- current unconstrained
  behaviour.

**4. Intra-plan smoothness** (`smoothness_weight`, added 2026-08-20) -- a
SOFT cost, same L1-linearization technique as mechanism 1 above, but
comparing each period against its own immediately preceding period WITHIN
THE SAME SOLVE, not against a previous solve's plan. Real, direct finding
that motivated this: a single solve's own battery_kw swung -1.25 -> -33.15
-> -0.30 kW across three consecutive 5-minute periods while the real
import price was byte-identical the whole time -- genuine LP degeneracy (a
run of economically-tied periods has no cost preference for WHICH exact
minute-by-minute shape delivers the same total energy), not a real
decision. Deliberately NOT the same fix as enabling max_rate_kw's own
intra-plan half: nimbus_solver_forecast_writer.py already explicitly
declines to use max_rate_kw specifically because a HARD cap risks
smearing a genuine, large, real transition (the 5pm P2P boundary). This
mechanism is a soft nudge instead -- sized small enough (see
DEFAULT_SMOOTHNESS_WEIGHT_KW's own docstring) to only ever break a
genuine tie, never override a real multi-dollar economic decision.
`smoothness_weight=0.0` (the default) disables it -- current unconstrained
behaviour, byte-identical to before this mechanism existed.

## SAME-PERIOD WASH-TRADE PREVENTION (2026-08-16)

Direct real-data finding: elements.py's own `GridConfig` used to reject,
at config time, any period where `export_price > import_price` (see
`MIN_GRID_COST_SPREAD`'s old docstring, still readable in git history).
That guard existed for a real reason -- if the LP is free to set
`grid_import[t]` and `grid_export[t]` BOTH large in the same period,
whenever export_price exceeds import_price it finds genuine (if
unphysical) free profit: import cheap, instantly resell high, absorb
the leftover into the battery. But a REAL household's genuine P2P sale
price legitimately, routinely exceeds import price during its own real
5pm-midnight window -- that IS the entire economic point of selling
P2P. The old guard couldn't tell that apart from the free-money loop,
and rejected (or, via a caller-side clamp workaround tried first,
neutered) the real signal outright -- confirmed live: an early build of
this solver, fed the real un-clamped $0.50/kWh P2P price, proposed
almost no discharge into the real P2P window at all, because the
caller had to suppress the real price down near import-price levels
just to get past config validation.

Investigated properly (not just re-clamped) and found the free-money
loop actually has TWO independent pathways, both needing to be closed
structurally for this to be safe with a real, uncapped price signal:

1. **Direct grid pathway**: `grid_export[t]` funded straight from a
   same-period `grid_import[t]`, with the difference absorbed into
   `charge[t]`. Closed by: `grid_export[t] <= solar_used[t] +
   discharge[t]` -- export can only ever be funded by real solar
   surplus or genuine battery discharge, never directly by import in
   the same period. This is also just a more physically correct model
   regardless of price: a real household meter reports one NET flow per
   interval, never simultaneous gross import AND export.

2. **Battery-routed pathway**: closing (1) alone is NOT sufficient --
   the LP can still charge[t] heavily (funded by grid_import[t]) and
   discharge[t] in the SAME period (funded by that same fresh charge,
   since the existing SoC equation only tracks the NET change across a
   period, with no real physical ordering within it), then let that
   discharge[t] legitimately satisfy constraint (1) above. Confirmed by
   hand-computation against this project's own real live numbers
   (charge_cost=0.005, discharge_cost=0.09, import~0.01-0.07,
   export~0.50): this pathway alone is still worth roughly $1/period in
   free profit even with (1) in place. Closed by a second constraint:
   `discharge[t] * hours[t] / discharge_efficiency <= soc[t-1] -
   min_soc_kwh` (using `battery.initial_soc_kwh` in place of `soc[-1]`
   for t=0) -- discharge in period t can only draw on SoC that
   genuinely existed BEFORE that period's own charging, never on
   energy added in the same period. Real, physically-motivated
   simplification (charge happens, THEN discharge draws from what was
   already there), standard in the battery-LP literature specifically
   to avoid this exact unrealistic instant-round-trip class of bug.

Together, (1) and (2) make ANY same-period import-to-export or
charge-to-discharge round trip infeasible, REGARDLESS of price --
closing the free-money loop structurally rather than by rejecting or
clamping the price data. Genuine ACROSS-TIME arbitrage (charge cheap
in period t, discharge to sell high in a LATER period t+k) is
completely unaffected -- `soc[t-1]` in constraint (2) correctly reflects
every earlier period's real accumulated charge, however many periods
back it happened, so this is exactly the real, desired behaviour the
LP should be free to discover.

Deliberately still pure LP, no MILP/binary variables -- confirmed via
careful case analysis that a general "at most one of grid_import[t],
grid_export[t] is nonzero" complementarity constraint is NOT
representable as a pure LP in general (this is a known result, not
something a clever reformulation can route around), but this
household's REAL structure (a battery genuinely sitting between the
grid and any export) means the two constraints above are both
necessary AND sufficient for THIS topology specifically, without ever
needing a general complementarity mechanism.

## MINIMUM TOTAL EXPORT COMMITMENT (2026-08-17)

`GridConfig.min_export_kwh` (default `None` -- complete no-op) adds one
extra constraint: `sum(grid_export[t] * hours[t]) >= min_export_kwh`
across the whole horizon. Expressed to LPProblem (which only has `<=`)
the same way AdequacyLoadConfig's own deadline constraint already is --
negate both sides.

Why this exists: found live, doing a real regret/EPR analysis against a
real household's own P2P export program. That program is a FIXED,
pre-committed nightly revenue (LocalVolts matches against the
household's own known historical dispatch pattern, not reactively
per-interval -- see the sibling 116KAT-HA-AI repo's own CLAUDE.md,
session 2026-08-09 "front-gap" investigation onward), not a plain
price-taking market. A first attempt modeled this as a flat export price
with NO volume cap -- the LP correctly, rationally exploited that as
free unlimited arbitrage (confirmed live: import price never exceeded
$0.316 the whole window against a flat $0.466 export price with no
ceiling, so the oracle wanted to cycle 204kWh through the battery
against a real ~10kWh load). Capping the volume by holding the REAL
settled P2P revenue as a FIXED credit (added identically to every
counterfactual, so it cancels out of every regret/EPR difference) fixed
the fake-arbitrage bug, but left a second, subtler gap: nothing forced a
perfect-foresight oracle to ALSO physically deliver the real committed
export volume to earn that fixed credit -- it could sit near-idle in the
residual (real spot export sits below real spot import most nights, so
there's no other reason to discharge) while still claiming the full
credit, which J_ach never got to do (it genuinely had to draw its own
SoC down, paying a real salvage-value opportunity cost, to deliver the
real matched volume). That gap systematically inflates regret / deflates
EPR for J* specifically. `min_export_kwh`, applied identically to
whichever scenarios are meant to represent "operating under this real
P2P commitment" (NOT the fully-idle J_ref reference case, which by
definition never enters into any export commitment at all), closes it:
the oracle is now forced to find the CHEAPEST way to deliver the same
real total export J_ach delivered, which is exactly the fair "how much
better could TIMING alone have done" comparison this analysis needs.

Genuinely unsatisfiable within the window's own `export_limit_kw *
sum(hours)` ceiling (or given the battery's own real energy capacity)
surfaces as a real `status="infeasible"` Plan, same as every other
structural constraint in this file -- not a silently-adjusted target.

## TWO-TIER EXPORT BONUS (2026-08-17)

`GridConfig.export_bonus_price`/`export_bonus_volume_kwh` (both `None` by
default -- complete no-op) model a real, confirmed household finding
that a flat blended export price gets fundamentally wrong: real P2P
revenue isn't "every kWh exported earns a diluted average rate," it's
"the first ~N kWh of real export each night earn close to the true
achieved rate (household-reported: 43-65c/kWh), anything beyond that
reverts to the much lower real spot rate." A caller that instead applies
a flat percentage discount uniformly (e.g. `match_fraction * p2p_rate +
(1-match_fraction) * spot_rate` on every kWh) systematically understates
the value of LATE-window export specifically -- confirmed live, this is
the direct, real cause of a household reporting the Solver's own
dispatch "landing prematurely" instead of continuing to sell hard right
to the edge of a real P2P window: a diluted-looking price gives the LP a
weaker reason to keep discharging late, when the true marginal revenue
of that late energy (if it lands within the real nightly volume cap) is
actually just as high as any earlier kWh.

Mechanically: `grid_export[t]` itself is completely unchanged -- still
the single real total-export variable used everywhere else in this file
(balance equation, wash-trade guards, stability mechanisms, reporting).
A separate `export_bonus[t]` variable, bounded by that SAME period's
real `grid_export[t]` (constraint 3, alongside the wash-trade guards --
can't claim bonus volume for export that didn't actually happen) and by
one cumulative constraint PER REAL CALENDAR DAY (`sum(export_bonus[t]*
hours[t]) <= export_bonus_volume_kwh`, for each day's own periods
separately -- NOT one constraint across the whole horizon; see the real
bug this caused, found and fixed the same day, right below the actual
constraint code), earns an EXTRA revenue credit of `export_bonus_price[t]`
on top of whatever `grid_export[t]` already earns at the base
`export_price[t]` rate. Since claiming bonus volume is
strictly free money whenever `export_bonus_price[t] > 0`, a revenue-
maximizing LP always claims as much of the capped bonus allocation as it
can, choosing WHICH real periods to claim it in based on genuine
economics -- not a crude, arbitrary even split -- naturally reproducing
"sell the real committed volume at the real rate, wherever in the window
that's most valuable to do, fall back to spot only once that's used up."

**3. Confidence-aware dispatch** (`risk_aversion`) -- adjusts which
NUMBER the LP treats as "the forecast" for Load/SheddableLoad/Solar
elements that carry a real `lower_kw`/`upper_kw` confidence band from
Nimbus's own Forecaster (see elements.py's own `_validate_confidence_band`
docstring). `risk_aversion=0.0` (default) uses the raw point forecast,
unchanged from before this existed. A load's risk-adjusted demand leans
toward its OWN upper bound (planning to actually have enough
battery/grid headroom even if the load draws more than the point
forecast suggests); solar's risk-adjusted supply leans toward its OWN
lower bound (not structurally under-provisioning backup capacity by
over-trusting solar that might not show up). Both lean amounts scale
with the band's own real width (a tight, confident forecast barely
moves; a wide, uncertain one moves more) AND with `risk_aversion`
(0.0 = fully trust the point forecast, 1.0 = fully plan for the
pessimistic bound). Elements with no band at all (`lower_kw is None`)
are completely unaffected regardless of `risk_aversion`'s value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
from numpy.typing import NDArray

from . import p2p_export
from .elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)
from .lp import LPProblem, LPResult

# Half of MIN_CHARGE_DISCHARGE_COST_SPREAD (elements.py) -- deliberately
# small enough that it can NEVER be mistaken for or override a genuine
# structural economic signal (the smallest real cost spread this
# codebase allows anywhere is 0.01 $/kWh), while still being large enough
# to reliably break an EXACT tie in the LP's own objective toward
# continuity rather than an arbitrary vertex. Only ever applied when a
# caller explicitly passes `previous_plan` -- see this module's own
# docstring for the full mechanism.
DEFAULT_PROXIMAL_WEIGHT_KW: float = 0.005

# Same principled derivation as DEFAULT_PROXIMAL_WEIGHT_KW just above (half
# of MIN_CHARGE_DISCHARGE_COST_SPREAD) -- same "small enough to never
# override a genuine economic signal" reasoning, applied to mechanism 4
# (intra-plan smoothness, see this module's own docstring) instead of
# mechanism 1. Deliberately the SAME value, not independently re-derived --
# both mechanisms answer the identical question ("is this deviation a real
# economic decision or an arbitrary tied vertex?"), just compared against a
# different reference point (the previous solve's plan vs. this solve's own
# immediately preceding period).
DEFAULT_SMOOTHNESS_WEIGHT_KW: float = 0.005

# nimbus issue #328 (Mark Purcell) -- multiplier applied to the LARGEST
# real $/kWh figure in play (peak import price, peak export price, and
# the highest terminal_value_breakpoints rate if configured) to derive
# the soft min/max-SoC penalty when a caller doesn't supply one
# explicitly. Needs to comfortably dominate every other $/kWh signal the
# LP sees so that (a) recovering toward min_soc is always more valuable
# than any real price arbitrage the LP could otherwise chase instead,
# and (b) the LP is never incentivised to deliberately let SoC drift
# below min_soc just to "unlock" more headroom in the terminal-value
# segment-fill construction below (see build_plan()'s own SoC-dynamics
# comment for the full reasoning on why this specific dominance
# property, not just "a big number", is what keeps that construction
# free of a real gaming vector). 10x is Mark's own proposed starting
# point, explicitly "not a defended value" in the issue -- taking the
# max across all three real price signals (rather than import price
# alone) is this implementation's own addition, since a household whose
# terminal-value rates or export prices happen to exceed its import
# price would otherwise get a penalty that doesn't actually dominate.
DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER: float = 10.0

# How close two periods' own real start times need to be to count as
# "the same real moment" for cross-solve alignment (proximal
# regularization, rate limiting). 1 second comfortably absorbs any
# floating-point drift from repeated `timedelta` addition in
# PeriodGrid.period_starts across many periods, while being far tighter
# than any real re-solve cadence this project would ever use (minutes,
# not seconds) -- so it can never accidentally align two genuinely
# different periods.
_ALIGNMENT_TOLERANCE: timedelta = timedelta(seconds=1)


@dataclass(frozen=True)
class SheddableLoadPlan:
    name: str
    served_kw: NDArray[np.float64]
    shed_kw: NDArray[np.float64]


@dataclass(frozen=True)
class AdequacyLoadPlan:
    """One adequacy load's own real result: how much power it was
    actually scheduled to draw each period, and the real cumulative
    energy delivered by its own deadline (should always be >=
    target_kwh whenever `plan.is_optimal` -- if it genuinely can't be
    met, the WHOLE solve reports infeasible, per AdequacyLoadConfig's
    own docstring; there is no partial/best-effort adequacy result).
    """

    name: str
    power_kw: NDArray[np.float64]
    delivered_by_deadline_kwh: float


@dataclass(frozen=True)
class Plan:
    """The solver's full output for one solve. Every array is indexed by
    period, same length as the PeriodGrid it was built from. `status` is
    always checked by the caller before trusting anything else here --
    see LPResult's own docstring for why infeasible/unbounded are real,
    expected outcomes this dataclass has to represent honestly, not paper
    over with zeros.
    """

    status: str
    periods: PeriodGrid
    battery_charge_kw: NDArray[np.float64]
    battery_discharge_kw: NDArray[np.float64]
    battery_soc_kwh: NDArray[np.float64]
    grid_import_kw: NDArray[np.float64]
    grid_export_kw: NDArray[np.float64]
    # How much of grid_export_kw[t] earned the two-tier export bonus (see
    # elements.py's own GridConfig.export_bonus_price docstring) --
    # always <= grid_export_kw[t] at every period, zero-filled whenever
    # the mechanism isn't active (the common case). Exposed as its own
    # field (not just folded into total_cost) specifically so a real
    # dashboard can show WHERE the real premium-rate volume actually
    # landed, not just the final dispatch numbers.
    export_bonus_kw: NDArray[np.float64]
    solar_used_kw: NDArray[np.float64]
    solar_curtailed_kw: NDArray[np.float64]
    sheddable_loads: list[SheddableLoadPlan]
    adequacy_loads: list[AdequacyLoadPlan]
    total_cost: float | None
    iterations: int
    # Shadow prices / binding-constraint diagnostics (2026-08-18), passed
    # straight through from LPResult -- see LPResult's own docstring for
    # what each means. Empty dicts (never populated) on a non-optimal
    # Plan, same "represent honestly, don't paper over" convention as
    # every other field here -- there's no meaningful shadow price for a
    # problem that was never actually solved.
    duals: dict[str, float] = field(default_factory=dict)
    reduced_costs: dict[str, float] = field(default_factory=dict)

    @property
    def is_optimal(self) -> bool:
        return self.status == "optimal"


def _align_previous_periods(
    periods: PeriodGrid, previous_plan: Plan | None
) -> dict[int, int]:
    """Map new-grid period index -> previous_plan period index, for every
    period whose REAL start time matches within _ALIGNMENT_TOLERANCE.
    Always returns a (possibly empty) dict, never None -- an empty dict
    is the single, uniform "nothing to align against" case, covering
    every one of: no previous_plan given, either grid lacking a real
    `start` anchor, a non-optimal previous_plan (see Plan.is_optimal --
    its arrays are meaningless zero-fills, not real previous decisions
    worth stabilizing toward), or the two horizons genuinely not
    overlapping in real time at all (e.g. the very first solve ever).
    Every downstream caller (proximal regularization, rate limiting)
    treats an empty dict identically to "this mechanism is off" -- no
    separate None-handling needed anywhere else in this file.
    """
    if previous_plan is None or not previous_plan.is_optimal:
        return {}
    new_starts = periods.period_starts
    old_starts = previous_plan.periods.period_starts
    if new_starts is None or old_starts is None:
        return {}
    mapping: dict[int, int] = {}
    for new_idx, new_t in enumerate(new_starts):
        for old_idx, old_t in enumerate(old_starts):
            if abs(new_t - old_t) <= _ALIGNMENT_TOLERANCE:
                mapping[new_idx] = old_idx
                break
    return mapping


def _risk_adjusted(
    forecast_kw: NDArray[np.float64],
    lower_kw: NDArray[np.float64] | None,
    upper_kw: NDArray[np.float64] | None,
    risk_aversion: float,
    *,
    conservative: str,
) -> NDArray[np.float64]:
    """Blend a point forecast toward its own pessimistic confidence bound,
    proportional to both the band's real width and `risk_aversion`. See
    this module's own docstring, mechanism 3, for the full reasoning.
    `conservative` is "upper" (loads -- pessimistic means MORE demand) or
    "lower" (solar -- pessimistic means LESS supply). Returns
    `forecast_kw` completely unchanged when no band is present or
    risk_aversion is exactly 0.0 -- this is what keeps every caller that
    doesn't use this mechanism byte-identical to before it existed.
    """
    if lower_kw is None or upper_kw is None or risk_aversion <= 0.0:
        return forecast_kw
    if conservative == "upper":
        return forecast_kw + risk_aversion * np.maximum(0.0, upper_kw - forecast_kw)
    return forecast_kw - risk_aversion * np.maximum(0.0, forecast_kw - lower_kw)


def _risk_adjusted_one_sided(
    forecast: NDArray[np.float64],
    bound: NDArray[np.float64] | None,
    risk_aversion: float,
    *,
    direction: str,
) -> NDArray[np.float64]:
    """Same blending as _risk_adjusted() above, but for a value that only
    ever has ONE meaningful pessimistic side (price -- "could be higher
    than forecast" for a buyer, "could be lower than forecast" for a
    seller), not a genuine lower+upper pair. _risk_adjusted() requires
    BOTH bounds to be non-None even though only one is ever used per
    call site; that's fine for solar/load (a real band naturally has
    both sides) but awkward here, so this is a clean, dedicated,
    single-bound version instead of forcing a dummy value into the
    unused side. `direction` is "up" (import price -- pessimistic means
    MORE expensive) or "down" (export price -- pessimistic means LESS
    revenue). Returns `forecast` completely unchanged when no bound is
    given or risk_aversion is exactly 0.0 -- same no-op guarantee as
    _risk_adjusted().
    """
    if bound is None or risk_aversion <= 0.0:
        return forecast
    if direction == "up":
        return forecast + risk_aversion * np.maximum(0.0, bound - forecast)
    return forecast - risk_aversion * np.maximum(0.0, forecast - bound)


def _add_proximal_penalty(
    p: LPProblem,
    var_names: list[str],
    family: str,
    alignment: dict[int, int],
    previous_values: NDArray[np.float64] | None,
    hours: NDArray[np.float64],
    proximal_weight: float,
) -> None:
    """Add the L1-linearized deviation penalty (mechanism 1, this
    module's own docstring) for one dispatch-variable family across every
    aligned period. No-op (adds nothing) when `alignment` is empty,
    `previous_values` is None, or `proximal_weight` is exactly 0.0 -- the
    common "mechanism not in use" case costs nothing extra in the built
    LP, not even unused variables. (`alignment` is guaranteed empty by
    `_align_previous_periods` whenever `previous_values` would be None,
    so the `is None` check here is a defensive backstop, not something
    normally reached.)
    """
    if proximal_weight <= 0.0 or not alignment or previous_values is None:
        return
    for new_idx, old_idx in alignment.items():
        prev_value = float(previous_values[old_idx])
        dev_pos = p.add_variable(
            f"prox_pos_{family}_{new_idx}",
            lb=0.0,
            cost=proximal_weight * hours[new_idx],
        )
        dev_neg = p.add_variable(
            f"prox_neg_{family}_{new_idx}",
            lb=0.0,
            cost=proximal_weight * hours[new_idx],
        )
        p.add_eq_constraint(
            {var_names[new_idx]: 1.0, dev_pos: -1.0, dev_neg: 1.0}, prev_value
        )


def _add_intraplan_smoothness_penalty(
    p: LPProblem,
    var_names: list[str],
    family: str,
    n: int,
    hours: NDArray[np.float64],
    smoothness_weight: float,
) -> None:
    """Mechanism 4 (2026-08-20, see this module's own docstring): an
    L1-linearized penalty (identical technique to _add_proximal_penalty
    above -- two nonnegative "deviation" variables, both costed, an
    equality constraint pinning their difference to the real delta) on how
    much one dispatch-variable family changes between EACH CONSECUTIVE PAIR
    of periods WITHIN THIS SAME SOLVE.

    Deliberately NOT the same thing as mechanism 1 (proximal_weight, which
    compares against a DIFFERENT solve's plan) or mechanism 2's intra-plan
    half (max_rate_kw, a HARD cap the production writer deliberately never
    enables -- see nimbus_solver_forecast_writer.py's own comment: a hard
    cap risks smearing a genuine, large, real transition like the 5pm P2P
    boundary). This is a SOFT cost instead, sized small enough (see
    DEFAULT_SMOOTHNESS_WEIGHT_KW's own docstring) to only ever break a
    genuine tie, never override a real price/cost difference -- so a real
    multi-dollar transition still happens sharply, while a run of
    economically-IDENTICAL adjacent periods (flat price, no real reason to
    prefer one minute-by-minute shape over another) gets nudged toward the
    smooth one instead of an arbitrary jagged vertex.

    Real household finding this exists to fix: a single solve's own
    battery_kw swung -1.25 -> -33.15 -> -0.30 kW across three consecutive
    5-minute periods while the real import price was byte-identical across
    all of them -- classic LP degeneracy, not a real decision.

    No-op (adds nothing) when smoothness_weight is exactly 0.0 -- the
    default, matching every other stability mechanism in this module.
    """
    if smoothness_weight <= 0.0:
        return
    for t in range(1, n):
        dev_pos = p.add_variable(
            f"smooth_pos_{family}_{t}", lb=0.0, cost=smoothness_weight * hours[t]
        )
        dev_neg = p.add_variable(
            f"smooth_neg_{family}_{t}", lb=0.0, cost=smoothness_weight * hours[t]
        )
        p.add_eq_constraint(
            {var_names[t]: 1.0, var_names[t - 1]: -1.0, dev_pos: -1.0, dev_neg: 1.0},
            0.0,
        )


def _add_rate_limit(
    p: LPProblem,
    var_names: list[str],
    family: str,
    n: int,
    alignment: dict[int, int],
    previous_values: NDArray[np.float64] | None,
    max_rate_kw: float,
) -> None:
    """Add the hard rate-limit constraints (mechanism 2, this module's
    own docstring) for one dispatch-variable family: period 0 bounded
    against the aligned previous-plan value (if any), every later period
    bounded against its own immediate predecessor within THIS solve.
    """
    if 0 in alignment and previous_values is not None:
        prev0 = float(previous_values[alignment[0]])
        p.add_ub_constraint({var_names[0]: 1.0}, prev0 + max_rate_kw)
        p.add_ub_constraint({var_names[0]: -1.0}, -(prev0 - max_rate_kw))
    for t in range(1, n):
        p.add_ub_constraint({var_names[t]: 1.0, var_names[t - 1]: -1.0}, max_rate_kw)
        p.add_ub_constraint({var_names[t]: -1.0, var_names[t - 1]: 1.0}, max_rate_kw)


def _infeasible_plan(periods: PeriodGrid, status: str, iterations: int) -> Plan:
    """A well-formed but empty Plan for a non-optimal solve -- every array
    present (zero-filled), never omitted, so a caller can always safely
    index into a Plan's arrays without a separate None-check first; the
    REAL signal to check is `status`/`is_optimal`, not array presence.
    """
    n = periods.n_periods
    # nimbus issue #356 (Mark Purcell): every field below used to alias
    # the SAME single np.zeros(n) array object -- verified live:
    # `plan.battery_charge_kw is plan.grid_import_kw` was True. Plan is
    # frozen=True (the dataclass itself is immutable), but that says
    # nothing about the arrays it holds -- any consumer doing in-place
    # arithmetic on one field of a non-optimal plan (a `+=`, or
    # `np.clip(..., out=...)`) would silently corrupt the other seven
    # fields too. A fresh zeros(n) per field removes the aliasing.
    return Plan(
        status=status,
        periods=periods,
        battery_charge_kw=np.zeros(n),
        battery_discharge_kw=np.zeros(n),
        battery_soc_kwh=np.zeros(n),
        grid_import_kw=np.zeros(n),
        grid_export_kw=np.zeros(n),
        export_bonus_kw=np.zeros(n),
        solar_used_kw=np.zeros(n),
        solar_curtailed_kw=np.zeros(n),
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=None,
        iterations=iterations,
        duals={},
        reduced_costs={},
    )


def build_plan(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    loads: list[LoadConfig] | None = None,
    sheddable_loads: list[SheddableLoadConfig] | None = None,
    adequacy_loads: list[AdequacyLoadConfig] | None = None,
    previous_plan: Plan | None = None,
    proximal_weight: float = DEFAULT_PROXIMAL_WEIGHT_KW,
    max_rate_kw: float | None = None,
    smoothness_weight: float = 0.0,
    risk_aversion: float = 0.0,
    import_price_risk_aversion: float = 0.0,
    export_price_risk_aversion: float = 0.0,
    soft_soc_penalty_per_kwh: float | None = None,
) -> Plan:
    """Build and solve one LP for the given horizon/inputs. Pure function --
    no I/O, no HA dependency, safe to call from anywhere including a plain
    local test script.

    `previous_plan`/`proximal_weight`/`max_rate_kw`/`risk_aversion` are
    the three cross-solve stability mechanisms -- see this module's own
    docstring for the full design. All default to "off" (a bare, single-
    solve LP, unchanged from before these existed).

    `import_price_risk_aversion`/`export_price_risk_aversion` (2026-08-21,
    split from a single `price_risk_aversion` scalar per direct Mark
    Purcell feedback -- see number.py's own comment for the full
    reasoning: "a single shared dial forces charge/discharge hedging to
    move together even though they're economically opposite decisions").
    Both are genuinely SEPARATE dials from `risk_aversion` above (which
    only ever hedges solar/load forecast error). Direct household
    finding: "the forecasts are always wrong but they tend to be more
    expensive in the afternoons, so waiting is not a good idea." Each
    uses its own half of GridConfig.import_price_upper/export_price_
    lower (both optional, None each = complete no-op) to bias the LP's
    OWN effective cost/revenue view of the future pessimistically --
    `import_price_risk_aversion` assumes import could cost more than the
    point forecast says (biasing the LP toward charging/importing
    sooner, before it might get worse); `export_price_risk_aversion`
    assumes export could earn less than the point forecast says (biasing
    the LP toward discharging/exporting sooner, before it might get
    worse) -- independently of each other and of `risk_aversion`,
    matching the explicit household ask for "more flexibility": trusting
    a load/solar forecast, trusting the import side of a price forecast,
    and trusting the export side of a price forecast are three genuinely
    different judgment calls.

    `soft_soc_penalty_per_kwh` (nimbus issue #328, Mark Purcell): min_soc/
    max_soc are SCHEDULING PREFERENCES the LP tries to respect and
    recover toward, not PHYSICAL INVARIANTS it can assume always hold --
    `battery.initial_soc_kwh` may legitimately arrive below min_soc_kwh
    (a template-averaged SoC sensor, a cold pack, a fresh install
    starting empty, sensor drift) or, in principle, above max_soc_kwh.
    `soc[t]` itself is only ever hard-bounded to the true physical range
    `[0, capacity_kwh]`; going outside `[min_soc_kwh, max_soc_kwh]` costs
    a real penalty (this parameter, per kWh per hour) instead of being
    impossible. `None` (the default) auto-derives the penalty from the
    real $/kWh signals already in this call -- see
    DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER's own comment for why it takes
    the max across import price, export price, AND any configured
    terminal_value_breakpoints rate, not import price alone. A caller
    that already knows a good value (e.g. a real historical peak import
    price across a longer window than this one solve sees) can pass it
    explicitly instead.

    When `battery.initial_soc_kwh` starts inside `[min_soc_kwh,
    max_soc_kwh]` and stays there for the whole horizon, this mechanism
    is a complete no-op -- the penalty terms all evaluate to exactly
    zero and the plan is numerically identical to the version of this
    function that hard-bounded `soc[t]` directly. It only ever engages
    for a genuinely below-floor (or above-ceiling) starting/drifting
    state, in which case the LP schedules real recovery (charging at
    cheap import windows, waiting through export windows) using whatever
    real price/solar/load context this solve actually has, rather than
    either crashing (the pre-#325 behaviour) or silently reporting a
    fictional in-range starting SoC (the #325/#327 clamp-and-pretend
    behaviour this mechanism replaces).
    """
    loads = loads or []
    sheddable_loads = sheddable_loads or []
    adequacy_loads = adequacy_loads or []
    n = periods.n_periods
    hours = periods.hours

    for cfg in (solar, *loads, *sheddable_loads):
        arr_name = "forecast_kw"
        arr = getattr(cfg, arr_name)
        if len(arr) != n:
            label = getattr(cfg, "name", cfg.__class__.__name__)
            msg = f"{label}: forecast_kw has {len(arr)} periods, expected {n} (PeriodGrid mismatch)"
            raise ValueError(msg)
    for arr, label in (
        (grid.import_price, "grid.import_price"),
        (grid.export_price, "grid.export_price"),
    ):
        if len(arr) != n:
            msg = f"{label} has {len(arr)} periods, expected {n} (PeriodGrid mismatch)"
            raise ValueError(msg)
    for al in adequacy_loads:
        if al.deadline_period >= n:
            msg = f"Adequacy load '{al.name}': deadline_period ({al.deadline_period}) is outside this PeriodGrid (0..{n - 1})"
            raise ValueError(msg)
    # nimbus issue #356 (Mark Purcell): elements.py's own BatteryConfig
    # validation checks terminal_value_period_indices for >= 0 and
    # duplicates, but can't check `< n` there -- it has no PeriodGrid to
    # check against at construction time (mirrors why deadline_period's
    # own bounds check, above, also lives here rather than on
    # AdequacyLoad itself). Without this, a stale index from a shorter
    # horizon (verified: [0, 99] on a 4-period grid) reaches soc[idx]
    # deep inside the terminal-value construction below as a raw,
    # unhelpful IndexError instead of a clear config error here.
    if battery.terminal_value_period_indices is not None:
        for idx in battery.terminal_value_period_indices:
            if idx >= n:
                msg = f"BatteryConfig.terminal_value_period_indices: index {idx} is outside this PeriodGrid (0..{n - 1})"
                raise ValueError(msg)

    alignment = _align_previous_periods(periods, previous_plan)

    if soft_soc_penalty_per_kwh is None:
        # See DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER's own comment for why
        # this takes the max across every real $/kWh signal in play, not
        # import price alone -- max(..., 0.01) is a genuine floor only,
        # for the degenerate all-zero-price case (e.g. a synthetic test
        # with every price at 0.0), so the penalty is never literally
        # zero and this mechanism can still do its job.
        candidate_rates = [
            float(np.max(grid.import_price)) if len(grid.import_price) else 0.0,
            float(np.max(grid.export_price)) if len(grid.export_price) else 0.0,
        ]
        if battery.terminal_value_breakpoints is not None:
            candidate_rates.append(
                max(rate for _width, rate in battery.terminal_value_breakpoints)
            )
        soft_soc_penalty_per_kwh = DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER * max(
            *candidate_rates, 0.01
        )

    p = LPProblem()

    # HARD gate against charging during a fixed_export_kw (P2P-committed)
    # period -- 2026-08-22, real live incident: with only grid_export[t]
    # pinned (below), nothing stopped the LP from ALSO importing grid
    # power and charging the battery during the same committed-export
    # period whenever the terminal-value mechanism's implied $/kWh for a
    # higher end-of-day SoC outweighed the real cost -- confirmed live via
    # the pushed forecast's own net_cost field actually going POSITIVE
    # during the charge (0.47-0.89 $/period) vs. the -0.08 to -0.42 $/period
    # it was making right before, i.e. this was never a real economically
    # rational trade the LP correctly found, it was the terminal-value
    # incentive overriding real near-term economics because charging was
    # simply never taken off the table. lb=ub=0.0 technique already used
    # elsewhere in this file for a disabled battery/charge (see the
    # adequacy-load comment below) -- makes this mathematically impossible
    # for the LP to choose, not just costed against, matching what the
    # REAL p2p_battery_sell_5pm_midnight automation already does (always
    # VPP-Discharge, never charge, for its entire committed window).
    charge = [
        p.add_variable(
            f"battery_charge_{t}",
            lb=0.0,
            ub=p2p_export.charging_ub_during_fixed_window(
                t, grid, battery.max_charge_kw
            ),
        )
        for t in range(n)
    ]
    discharge = [
        p.add_variable(f"battery_discharge_{t}", lb=0.0, ub=battery.max_discharge_kw)
        for t in range(n)
    ]
    # nimbus issue #328 (Mark Purcell): soc[t]'s only HARD bound is now
    # the true physical range [0, capacity_kwh] -- min_soc_kwh/max_soc_kwh
    # are enforced as a SOFT preference via underfill/overfill below, not
    # a bound on this variable itself. See build_plan()'s own docstring
    # ("soft_soc_penalty_per_kwh") for the full design and why this
    # replaces the pre-#328 hard bound.
    soc = [
        p.add_variable(f"battery_soc_{t}", lb=0.0, ub=battery.capacity_kwh)
        for t in range(n)
    ]
    # underfill[t] = max(0, min_soc_kwh - soc[t]), overfill[t] = max(0,
    # soc[t] - max_soc_kwh) -- both genuinely pinned to their exact
    # max(0, ...) value (not just upper-bounded) because they're COSTED
    # below: minimizing total cost always drives a costed, otherwise-
    # unconstrained-from-above slack variable down to the smallest value
    # its own constraint permits, which is exactly the true violation
    # amount. This same "pinned by cost + one-sided inequality" property
    # is what makes it safe to reuse underfill[idx] inside the terminal-
    # value segment-fill construction and the discharge wash-trade guard
    # further below, instead of just being a standalone floor penalty --
    # see each of those sites' own comments for why a naive re-relaxation
    # there would otherwise reopen a real gaming vector (the LP could
    # otherwise "unlock" extra terminal-value credit, or extra discharge
    # headroom, by pretending SoC is lower than it really is).
    underfill = [
        p.add_variable(f"battery_soc_underfill_{t}", lb=0.0, ub=battery.min_soc_kwh)
        for t in range(n)
    ]
    overfill = [
        p.add_variable(
            f"battery_soc_overfill_{t}",
            lb=0.0,
            ub=battery.capacity_kwh - battery.max_soc_kwh,
        )
        for t in range(n)
    ]
    for t in range(n):
        # soc[t] + underfill[t] >= min_soc_kwh
        p.add_ub_constraint({soc[t]: -1.0, underfill[t]: -1.0}, -battery.min_soc_kwh)
        # soc[t] - overfill[t] <= max_soc_kwh
        p.add_ub_constraint({soc[t]: 1.0, overfill[t]: -1.0}, battery.max_soc_kwh)
        # nimbus issue #338: the penalty is a bare $/kWh on the STATE
        # violation, deliberately NOT scaled by hours[t]. Every signal
        # the "penalty dominates" argument above has to beat is itself a
        # bare $/kWh on an energy quantity -- the terminal-value segment
        # credit (-rate * scale, unscaled by period length) and the
        # discharge headroom the wash-trade guard hands out per kWh of
        # underfill. Scaling only this side by hours[t] made dominance a
        # function of the grid: safe on a 1 h grid (10x margin), broken
        # on the production 5-minute grid (0.83x -- the LP could inflate
        # underfill[n-1] to its ub and bank phantom terminal credit,
        # then sell real stored energy it should have held). A state
        # penalty per period is also the right physics: being below the
        # floor at a 5-minute checkpoint is exactly as much of a
        # violation as being below it at an hourly one.
        p.set_cost(underfill[t], soft_soc_penalty_per_kwh)
        p.set_cost(overfill[t], soft_soc_penalty_per_kwh)
    grid_import = [
        p.add_variable(f"grid_import_{t}", lb=0.0, ub=grid.import_limit_kw)
        for t in range(n)
    ]
    # fixed_export_kw (see elements.py's own GridConfig docstring for the
    # full "P2P needs a constant, pre-committed rate, not a price-chased
    # one" finding) -- a period with a real (non-NaN) fixed value gets
    # BOTH lb and ub of its own grid_export[t] variable pinned to exactly
    # that number at construction time, forcing the LP to treat that
    # period's export rate as a given rather than a free decision. Every
    # other period (fixed_export_kw is None, or that period's own entry
    # is NaN) keeps the normal [0, export_limit_kw] bounds, byte-
    # identical to before this field existed.
    grid_export = []
    for t in range(n):
        export_lb, export_ub = p2p_export.grid_export_bounds(
            t, grid, grid.export_limit_kw
        )
        grid_export.append(
            p.add_variable(f"grid_export_{t}", lb=export_lb, ub=export_ub)
        )

    # Two-tier export bonus (see elements.py's own GridConfig docstring,
    # "export_bonus_price / export_bonus_volume_kwh") -- export_bonus[t]
    # is bounded by grid_export[t] itself just below (added as a real
    # constraint, not a variable upper bound, since grid_export[t] is
    # itself a variable not a constant); the cumulative volume cap is
    # added further below alongside the other whole-horizon constraints.
    has_export_bonus = p2p_export.has_export_bonus(grid)
    export_bonus = (
        [
            p2p_export.add_export_bonus_variable(
                p, f"export_bonus_{t}", grid.export_limit_kw
            )
            for t in range(n)
        ]
        if has_export_bonus
        else None
    )

    # Mechanism 3 (confidence-aware dispatch): solar's own EFFECTIVE
    # ceiling for what the LP can count on -- risk_aversion=0.0 or no
    # band present leaves this identical to solar.forecast_kw.
    effective_solar_kw = _risk_adjusted(
        solar.forecast_kw,
        solar.lower_kw,
        solar.upper_kw,
        risk_aversion,
        conservative="lower",
    )
    solar_used = [
        p.add_variable(f"solar_used_{t}", lb=0.0, ub=float(effective_solar_kw[t]))
        for t in range(n)
    ]

    # Price-risk hedging (see this function's own docstring for the full
    # "afternoons tend to run more expensive than forecast" household
    # finding, and the 2026-08-21 import/export split reasoning) -- each
    # side's own risk_aversion=0.0 or no band present leaves that side
    # identical to grid.import_price/export_price, used below in place of
    # the raw arrays wherever the LP's own cost/revenue is set.
    effective_import_price = _risk_adjusted_one_sided(
        grid.import_price,
        grid.import_price_upper,
        import_price_risk_aversion,
        direction="up",
    )
    effective_export_price = _risk_adjusted_one_sided(
        grid.export_price,
        grid.export_price_lower,
        export_price_risk_aversion,
        direction="down",
    )

    # Mechanism 3 continued: sheddable loads' own effective (pessimistic-
    # leaning) demand -- both the shed ceiling and the balance-equation
    # contribution are computed from this, so "how much of this load MUST
    # stay served" scales consistently with whatever the LP is actually
    # planning to serve. Reporting (served_kw/shed_kw below) still uses
    # the RAW forecast, matching solar_curtailed_kw's own treatment.
    shed_vars: dict[str, list[str]] = {}
    effective_shed_forecast: dict[str, NDArray[np.float64]] = {}
    for sl in sheddable_loads:
        eff = _risk_adjusted(
            sl.forecast_kw,
            sl.lower_kw,
            sl.upper_kw,
            risk_aversion,
            conservative="upper",
        )
        effective_shed_forecast[sl.name] = eff
        max_shed = [(1.0 - sl.min_fraction) * float(eff[t]) for t in range(n)]
        shed_vars[sl.name] = [
            p.add_variable(f"shed_{sl.name}_{t}", lb=0.0, ub=max_shed[t])
            for t in range(n)
        ]

    # Adequacy loads (2026-08-16, direct response to real feedback -- see
    # AdequacyLoadConfig's own docstring). No forecast at all: a power
    # variable per period, forced to exactly 0 outside
    # [earliest_period, deadline_period] via a zero-width bound (same
    # lb=ub=0.0 technique already used elsewhere in this file for a
    # disabled battery/charge), free to be anything in [0, max_power_kw]
    # within the window -- the LP itself decides WHEN to run it, subject
    # only to the deadline constraint added below (real cost zero unless
    # a caller explicitly costs it via a load-specific mechanism, none
    # exists yet -- see the module's own "not yet built" notes).
    adequacy_vars: dict[str, list[str]] = {}
    for al in adequacy_loads:
        adequacy_vars[al.name] = [
            p.add_variable(
                f"adequacy_{al.name}_{t}",
                lb=0.0,
                ub=al.max_power_kw
                if al.earliest_period <= t <= al.deadline_period
                else 0.0,
            )
            for t in range(n)
        ]

    # ---- Cost terms ----
    # battery.charge_cost/discharge_cost may be a plain scalar (applied
    # identically to every period) or a real per-period array (2026-08-16,
    # see BatteryConfig's own docstring) -- np.broadcast_to normalizes
    # both cases to a real length-n array up front, so the loop below
    # never needs to know which form the caller passed. A caller-supplied
    # array whose own length doesn't match n raises here (a clear numpy
    # broadcast error), not silently later.
    charge_cost_arr = np.broadcast_to(
        np.asarray(battery.charge_cost, dtype=np.float64), (n,)
    )
    discharge_cost_arr = np.broadcast_to(
        np.asarray(battery.discharge_cost, dtype=np.float64), (n,)
    )
    # Real economic cycle-wear cost (Track B2, elements.py's own
    # degradation_cost_per_kwh -- see that field's own docstring for the
    # full "why a separate additive term, not folded into charge_cost/
    # discharge_cost" reasoning). set_cost() is additive (see lp.py's
    # own docstring), so this simply layers on top of whatever TOU-
    # driven charge_cost/discharge_cost already priced -- 0.0 (the
    # default) is a genuine no-op, adds nothing to either cost.
    for t in range(n):
        p.set_cost(grid_import[t], effective_import_price[t] * hours[t])
        p.set_cost(grid_export[t], -effective_export_price[t] * hours[t])
        p.set_cost(
            charge[t],
            (charge_cost_arr[t] + battery.degradation_cost_per_kwh) * hours[t],
        )
        p.set_cost(
            discharge[t],
            (discharge_cost_arr[t] + battery.degradation_cost_per_kwh) * hours[t],
        )
        for sl in sheddable_loads:
            p.set_cost(shed_vars[sl.name][t], sl.shed_cost * hours[t])
    # Two-tier export bonus (see elements.py's own GridConfig docstring):
    # export_bonus[t] earns an EXTRA revenue credit on top of whatever
    # grid_export[t] already earns at the base rate above -- set_cost()
    # ADDS to an existing coefficient (see its own docstring, same
    # pattern already used for salvage_value/headroom_value below), but
    # export_bonus[t] is its own separate variable here, not sharing
    # grid_export[t]'s coefficient, so this is a plain new cost, not an
    # accumulation.
    if has_export_bonus:
        for t in range(n):
            p2p_export.set_export_bonus_cost(p, export_bonus[t], t, grid, hours)
    if battery.terminal_value_breakpoints is not None:
        # Piecewise-linear concave terminal value (2026-08-18, Mark
        # Purcell's audit item #7 -- see BatteryConfig's own docstring
        # for the full "hard corner" problem this replaces, and its own
        # terminal_value_breakpoints docstring for why non-increasing
        # rates make this construction behave concavely with no explicit
        # ordering constraint needed). One small variable per breakpoint
        # per applied period index (negligible LP cost regardless of
        # horizon length), each summing to exactly soc[idx] - min_soc_kwh
        # -- every kWh above the floor priced exactly once, each at its
        # own segment's rate.
        #
        # Applied at every index in terminal_value_period_indices
        # (2026-08-22, real household finding -- see that field's own
        # docstring in elements.py) instead of hardcoded to just n-1:
        # None (the default) preserves the exact original single-final-
        # period behaviour, byte-identical to every scenario built before
        # this extension existed.
        period_indices = (
            battery.terminal_value_period_indices
            if battery.terminal_value_period_indices is not None
            else [n - 1]
        )
        # Real bug found live (Mark Purcell, nimbus #144, 2026-08-24):
        # applying the SAME full-strength curve at EVERY checkpoint
        # (2026-08-22's own fix, above) let the SAME physical stored
        # energy earn a full terminal-value credit at EVERY midnight it
        # survived through, not once. Confirmed empirically (a controlled
        # scenario, horizon and prices held fixed, only the checkpoint
        # COUNT varied): SoC held at a point hours before ANY checkpoint
        # jumped from the real floor to full capacity the moment a
        # SECOND checkpoint was added later in the same horizon, purely
        # from that downstream credit -- and the LP's own reported
        # total_cost got monotonically "better" as more checkpoints were
        # added, the tell-tale sign of the same energy being credited
        # more than once. On a real 4-day horizon (4 real midnights + the
        # true final period = 5 checkpoints) this manifested as the
        # battery refusing to discharge at a genuinely profitable price
        # for hours, holding a ~4x-inflated effective marginal value.
        #
        # Fix: only the TRUE final period (n-1) -- the one, real "the
        # LP's own visibility ends here" moment -- gets the FULL,
        # unscaled curve. Every other (intermediate day-boundary)
        # checkpoint gets the curve scaled down by 1/(number of
        # intermediate checkpoints), so the cumulative "carry into
        # tomorrow" incentive a single unit of energy could ever collect
        # by surviving through ALL of them stays bounded to roughly one
        # terminal-value-equivalent in total, not one PER checkpoint.
        # With exactly one intermediate checkpoint (the shape this
        # project's own existing test suite already validates,
        # test_solver_terminal_value_checkpoints.py) the scale factor is
        # exactly 1.0 -- this fix changes nothing for that case, it only
        # engages once there are 2+ intermediate checkpoints, which is
        # precisely where the compounding becomes severe.
        n_intermediate = sum(1 for idx in period_indices if idx != n - 1)
        for idx in period_indices:
            scale = 1.0 if idx == n - 1 or n_intermediate == 0 else 1.0 / n_intermediate
            seg_vars = [
                p.add_variable(f"terminal_seg_{idx}_{i}", lb=0.0, ub=width)
                for i, (width, _rate) in enumerate(battery.terminal_value_breakpoints)
            ]
            # nimbus issue #328: with soc[idx] now allowed below
            # min_soc_kwh (see the soc/underfill/overfill construction
            # above), the original `sum(seg_vars) = soc[idx] -
            # min_soc_kwh` equality would go negative whenever soc[idx]
            # is genuinely below the floor -- infeasible outright, since
            # every seg_var has lb=0. Folding in underfill[idx] (already
            # pinned to exactly max(0, min_soc_kwh - soc[idx]) by its own
            # cost, see the comment where it's defined) fixes this
            # WITHOUT reopening a gaming vector: when soc[idx] >=
            # min_soc_kwh, underfill[idx] is driven to exactly 0 by its
            # own penalty (nothing to gain by leaving it nonzero), so
            # this reduces to the original equation unchanged. When
            # soc[idx] < min_soc_kwh, underfill[idx] is pinned to exactly
            # (min_soc_kwh - soc[idx]) the same way, making the RHS
            # exactly 0 -- seg_vars are forced to sum to zero, i.e. ZERO
            # terminal-value credit claimed for energy that doesn't
            # genuinely exist above the floor. The LP cannot profitably
            # inflate underfill[idx] to "unlock" more seg_var room,
            # because underfill's own per-kWh penalty
            # (soft_soc_penalty_per_kwh, dominant by construction -- see
            # DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER) always costs strictly
            # more than any terminal_value_breakpoints rate could credit
            # back.
            p.add_eq_constraint(
                {
                    **{seg: 1.0 for seg in seg_vars},
                    soc[idx]: -1.0,
                    underfill[idx]: -1.0,
                },
                -battery.min_soc_kwh,
                name=f"terminal_value_segments_fill_{idx}",
            )
            for seg, (_width, rate) in zip(
                seg_vars, battery.terminal_value_breakpoints, strict=True
            ):
                p.set_cost(seg, -rate * scale)
    else:
        # Salvage value: a one-time credit on the FINAL period's soc -- without
        # this, a finite-horizon LP has no reason to ever hold charge past the
        # last period it can see, and will always drain to its own min_soc on
        # the final tick (see the architecture sketch's own §6 "Salvage value,
        # in plain terms" explainer).
        p.set_cost(soc[n - 1], -battery.salvage_value)
        # Headroom value (2026-08-16, direct response to real feedback -- see
        # BatteryConfig's own docstring for the full "option value of energy
        # AND of headroom" reasoning): -headroom_value * (max_soc - soc[n-1])
        # expands to a CONSTANT (-headroom_value*max_soc, doesn't affect the
        # optimal solution -- LP optimization is invariant to a constant
        # objective offset) plus +headroom_value*soc[n-1]. set_cost() already
        # ADDS to soc[n-1]'s existing coefficient (see its own docstring), so
        # this second call is the correct, minimal way to combine both terms
        # -- net terminal coefficient becomes -(salvage_value - headroom_value).
        # headroom_value=0.0 (the default) adds exactly zero, byte-identical
        # to every scenario built before this field existed.
        p.set_cost(soc[n - 1], battery.headroom_value)

    # ---- Stability mechanisms 1 & 2 (see module docstring) ----
    prev_charge = previous_plan.battery_charge_kw if previous_plan is not None else None
    prev_discharge = (
        previous_plan.battery_discharge_kw if previous_plan is not None else None
    )
    prev_grid_import = (
        previous_plan.grid_import_kw if previous_plan is not None else None
    )
    prev_grid_export = (
        previous_plan.grid_export_kw if previous_plan is not None else None
    )
    for var_names, family, prev_values in (
        (charge, "charge", prev_charge),
        (discharge, "discharge", prev_discharge),
        (grid_import, "grid_import", prev_grid_import),
        (grid_export, "grid_export", prev_grid_export),
    ):
        _add_proximal_penalty(
            p, var_names, family, alignment, prev_values, hours, proximal_weight
        )
        if max_rate_kw is not None:
            _add_rate_limit(
                p, var_names, family, n, alignment, prev_values, max_rate_kw
            )
        _add_intraplan_smoothness_penalty(
            p, var_names, family, n, hours, smoothness_weight
        )

    # ---- SoC dynamics ----
    for t in range(n):
        prev = battery.initial_soc_kwh if t == 0 else None
        terms = {
            soc[t]: 1.0,
            charge[t]: -battery.charge_efficiency * hours[t],
            discharge[t]: hours[t] / battery.discharge_efficiency,
        }
        if prev is None:
            terms[soc[t - 1]] = -1.0
            p.add_eq_constraint(terms, 0.0)
        else:
            p.add_eq_constraint(terms, prev)

    # ---- Power balance at the switchboard, every period ----
    # Mechanism 3 continued: plain loads' own effective (pessimistic-
    # leaning) demand -- see the shed-load treatment above, same reasoning.
    plain_load_total = np.zeros(n)
    for load in loads:
        plain_load_total += _risk_adjusted(
            load.forecast_kw,
            load.lower_kw,
            load.upper_kw,
            risk_aversion,
            conservative="upper",
        )

    for t in range(n):
        terms = {
            solar_used[t]: 1.0,
            discharge[t]: 1.0,
            grid_import[t]: 1.0,
            charge[t]: -1.0,
            grid_export[t]: -1.0,
        }
        rhs = plain_load_total[t]
        for sl in sheddable_loads:
            # served = forecast - shed, moved to the LHS as -shed (a
            # positive coefficient on the shed variable subtracts from
            # what the balance equation demands be supplied)
            terms[shed_vars[sl.name][t]] = 1.0
            rhs += float(effective_shed_forecast[sl.name][t])
        for al in adequacy_loads:
            # An adequacy load's own scheduled power is real demand at
            # the switchboard -- a NEGATIVE LHS coefficient (unlike
            # solar_used/discharge/grid_import, which are +1 SUPPLY
            # terms), the same sign convention as charge/grid_export
            # (things that consume rather than provide net supply).
            # Real bug caught by this file's own dedicated test: using
            # +1.0 here made the balance equation get EASIER to satisfy
            # as adequacy_power increased, which is backwards -- it
            # forced discharge+grid_import+adequacy_power to sum to
            # exactly the (here, zero) base demand, making any real
            # target infeasible outright.
            terms[adequacy_vars[al.name][t]] = -1.0
        # Named (2026-08-18) so its dual value -- the real-time shadow
        # price of energy at this period, exactly what a live spot/P2P
        # rate is supposed to approximate -- is directly readable rather
        # than an anonymous row index. The single most economically
        # meaningful dual in this whole model.
        p.add_eq_constraint(terms, rhs, name=f"power_balance_t{t}")

    # ---- Same-period wash-trade prevention (see module docstring,
    # "SAME-PERIOD WASH-TRADE PREVENTION" -- two structural constraints,
    # both required, closing the two independent pathways found via real
    # household data) ----
    for t in range(n):
        # (1) Direct grid pathway: export can only be funded by real
        # solar surplus or genuine battery discharge, never a same-period
        # grid_import[t] -- grid_export[t] - solar_used[t] - discharge[t] <= 0.
        p.add_ub_constraint(
            {grid_export[t]: 1.0, solar_used[t]: -1.0, discharge[t]: -1.0}, 0.0
        )
        # (2) Battery-routed pathway: discharge[t] can only draw on SoC
        # that genuinely existed BEFORE this period's own charging, never
        # energy added within the same period -- discharge[t]*hours[t]/
        # discharge_efficiency <= soc[t-1] - min_soc_kwh (battery.initial_
        # soc_kwh stands in for soc[-1] at t=0, a known constant, so it
        # moves straight to the RHS rather than needing a variable term).
        #
        # nimbus issue #328: as originally written, this constraint
        # implicitly forced soc[t-1] >= min_soc_kwh for ALL t -- even at
        # discharge[t]=0 (its own lb), satisfying the inequality still
        # required soc[t-1]-min_soc_kwh >= 0, silently reintroducing a
        # hard floor the soc[]/underfill[]/overfill[] relaxation above
        # was specifically built to remove. Fixed the same way as the
        # terminal-value segment-fill equality above: fold in
        # underfill[t-1] (pinned to exactly max(0, min_soc_kwh -
        # soc[t-1]) by its own cost). When soc[t-1] >= min_soc_kwh this
        # is unchanged (underfill[t-1] pinned to 0). When soc[t-1] is
        # genuinely below the floor, the RHS collapses to exactly 0,
        # forcing discharge[t]=0 -- the LP correctly cannot discharge
        # energy that doesn't exist above the floor, and must recover
        # (via charging or waiting) before it can discharge again, which
        # is exactly the intended "schedule recovery, don't pretend"
        # behaviour. At t=0 there is no underfill[-1] variable --
        # battery.initial_soc_kwh is a known constant, so the equivalent
        # max(0, ...) is computed directly in Python rather than via an
        # LP variable, with the identical effect.
        draw_coeff = hours[t] / battery.discharge_efficiency
        if t == 0:
            p.add_ub_constraint(
                {discharge[t]: draw_coeff},
                max(0.0, battery.initial_soc_kwh - battery.min_soc_kwh),
            )
        else:
            p.add_ub_constraint(
                {discharge[t]: draw_coeff, soc[t - 1]: -1.0, underfill[t - 1]: -1.0},
                -battery.min_soc_kwh,
            )
        # (3) Combined-direction cap (nimbus issue #245): the physical
        # battery has one DC current direction at any instant -- it cannot
        # charge and discharge simultaneously, so charge[t] and discharge[t]
        # (independent LP variables with no link between them otherwise)
        # left an unconstrained degeneracy budget wide open. A bad upstream
        # price signal (nimbus issue #236) let the LP inflate both freely in
        # the same period -- e.g. charge=17.98 + discharge=16.91 kW, netting
        # to the real -1.06 kW charge the LP had actually decided on, with
        # the rest pure wash-trade noise nothing pinned down. This single
        # linear constraint kills that budget without a MILP reformulation:
        # charge[t] + discharge[t] <= max(max_charge_kw, max_discharge_kw).
        # On any normal row only one side is ever nonzero, so the cap sits
        # above both individual ub's already in force and changes nothing;
        # it only binds on a wash-trade row, forcing the LP back to its real
        # net. (A true `charge[t]*discharge[t] == 0` complementarity needs a
        # binary per period -- MILP, tracked separately as issue #238 -- but
        # the objective already has no incentive for simultaneous nonzero
        # once #242 landed, so this linear cap is sufficient in practice.)
        p.add_ub_constraint(
            {charge[t]: 1.0, discharge[t]: 1.0},
            max(battery.max_charge_kw, battery.max_discharge_kw),
        )
        # (4) Two-tier export bonus (see elements.py's own GridConfig
        # docstring): export_bonus[t] can never exceed that SAME period's
        # real total export[t] -- can't claim bonus volume for export
        # that never actually happened -- export_bonus[t] - grid_export[t]
        # <= 0.
        if has_export_bonus:
            p2p_export.add_export_bonus_le_export_constraint(
                p, export_bonus[t], grid_export[t]
            )
        # (5) Combined grid-direction cap (nimbus issue #266): constraints
        # (1)+(2) above close the SAME-PERIOD WASH-TRADE pathway (import
        # funding export via a fresh charge-then-discharge round trip
        # within one period) but do NOT close a real, different gap --
        # grid_import[t] funding charge[t] while an entirely separate,
        # already-existing SoC (accumulated in an EARLIER period, so (2)
        # never blocks it) simultaneously discharges to fund grid_export[t]
        # in that SAME period. Neither leg is a wash trade at the LP-
        # accounting level (the imported energy and the exported energy
        # are genuinely different electrons, logically speaking), but a
        # real household's single grid connection can only carry current
        # in one direction at any instant -- confirmed live (Mark
        # Purcell): a real capture showed grid_import_kw=13.133 and
        # grid_export_kw=30.0 simultaneously in the identical period,
        # reproduced again (import=6.897/export=30.0, a smaller but still
        # real violation) by replaying the exact same real solar/load/
        # price inputs through this file's own (1)-(4) constraints alone
        # -- i.e. this gap is NOT closed by (1)-(4), confirmed empirically
        # before writing this fix, not assumed.
        #
        # Same technique as (3)'s own battery-side cap, same honest
        # caveat: bounds the combined magnitude, does not fully eliminate
        # every possible simultaneous-nonzero case (a true `grid_import[t]
        # * grid_export[t] == 0` complementarity needs a binary per
        # period -- MILP, tracked separately as issue #238) --
        # grid_import[t] + grid_export[t] <= max(import_limit_kw,
        # export_limit_kw). On any normal row only one side is ever
        # meaningfully nonzero, so the cap sits above both individual
        # ub's already in force and changes nothing there; it only binds
        # on a row exploiting this gap, forcing the LP back toward a
        # single real net direction.
        p.add_ub_constraint(
            {grid_import[t]: 1.0, grid_export[t]: 1.0},
            max(grid.import_limit_kw, grid.export_limit_kw),
        )

    # ---- SoC-dependent power curves (see BatteryConfig's own
    # charge_power_curve/discharge_power_curve docstring for the full
    # "real CC->CV charge taper near full, BMS-precision caution near
    # empty" reasoning). None (the default, either field) adds nothing
    # here -- charge[t]/discharge[t] stay bounded only by the flat
    # ub=max_charge_kw/max_discharge_kw already set at their own
    # construction above, byte-identical to every scenario built before
    # these fields existed.
    #
    # When provided: standard LP technique for a concave piecewise-
    # linear UPPER BOUND -- the true achievable power at any soc equals
    # the MINIMUM, over every curve segment, of that segment's own line
    # (intercept + slope*soc) extended in both directions. Expressed as
    # one <= constraint PER SEGMENT PER PERIOD on the already-existing
    # charge[t]/discharge[t] and soc[t-1] variables -- no new LP
    # variables at all (unlike terminal_value_breakpoints), so real LP
    # growth here is purely additional constraint rows, nothing more.
    # soc[t-1] is battery.initial_soc_kwh (a known constant) at t==0,
    # same convention as the wash-trade-prevention constraint (2) just
    # above -- moves straight to the RHS rather than needing a variable
    # term.
    for var_list, curve in (
        (charge, battery.charge_power_curve),
        (discharge, battery.discharge_power_curve),
    ):
        if curve is None:
            continue
        socs = [s for s, _pw in curve]
        powers = [pw for _s, pw in curve]
        for seg_i in range(len(curve) - 1):
            slope = (powers[seg_i + 1] - powers[seg_i]) / (
                socs[seg_i + 1] - socs[seg_i]
            )
            intercept = powers[seg_i] - slope * socs[seg_i]
            for t in range(n):
                if t == 0:
                    rhs = intercept + slope * battery.initial_soc_kwh
                    p.add_ub_constraint({var_list[t]: 1.0}, rhs)
                else:
                    p.add_ub_constraint(
                        {var_list[t]: 1.0, soc[t - 1]: -slope}, intercept
                    )

    # ---- Adequacy deadline constraints -- one inequality per adequacy
    # load, NOT per period: cumulative energy delivered through the
    # deadline must reach target_kwh. LPProblem only has <=, so this is
    # expressed as -sum(power*hours) <= -target_kwh. Genuinely
    # unsatisfiable within the window (see AdequacyLoadConfig's own
    # docstring) surfaces as a real status="infeasible" Plan below, not
    # a silently-adjusted target.
    for al in adequacy_loads:
        window = range(al.earliest_period, al.deadline_period + 1)
        terms = {adequacy_vars[al.name][t]: -hours[t] for t in window}
        # Named (2026-08-18) -- its dual is the marginal cost of this
        # specific deadline, e.g. "how much cheaper would the plan be if
        # this load had one more hour to finish."
        p.add_ub_constraint(terms, -al.target_kwh, name=f"adequacy_deadline_{al.name}")

    # ---- Minimum total export commitment (see module docstring,
    # "MINIMUM TOTAL EXPORT COMMITMENT") -- sum(export*hours) >=
    # min_export_kwh, expressed as <= by negating both sides (same
    # technique as the adequacy deadline constraint just above). No-op
    # when grid.min_export_kwh is None (the default).
    if grid.min_export_kwh is not None:
        terms = {grid_export[t]: -hours[t] for t in range(n)}
        # Named (2026-08-18) -- its dual is the marginal cost of this
        # commitment floor, e.g. "how much cheaper would tonight's plan be
        # without this minimum-export requirement."
        p.add_ub_constraint(terms, -grid.min_export_kwh, name="min_export_commitment")

    # ---- Two-tier export bonus cumulative cap (see elements.py's own
    # GridConfig docstring) -- sum(export_bonus[t]*hours[t]) <=
    # export_bonus_volume_kwh, applied SEPARATELY PER REAL CALENDAR DAY,
    # not once across the whole horizon. No-op when grid.export_bonus_*
    # is None (the default).
    #
    # Real bug found and fixed the same day this feature was built
    # (2026-08-17): a single global cap across a multi-day horizon lets
    # the LP greedily front-load the ENTIRE bonus allocation into the
    # very first real P2P window it sees, then behave as if it's
    # permanently exhausted its P2P eligibility for every later night --
    # confirmed live: night 1 correctly sold cleanly all evening, but
    # nights 2-4 progressively collapsed, night 4 showing ZERO export the
    # entire window. Real P2P settlement resets every single night, not
    # once per multi-day horizon -- the cap needs to mean "up to N kWh
    # PER DAY", not "up to N kWh, ever, across however many days this
    # solve happens to look at."
    #
    # Grouped by `periods.period_starts`' own real calendar date. Falls
    # back to ONE global constraint (the previous behaviour) when the
    # grid has no calendar anchor (`periods.start is None`) -- there's no
    # way to know where a real day boundary falls without real
    # timestamps, so a single conservative cap is the only honest option
    # in that case, not a silent behaviour change.
    if has_export_bonus:
        # Per-real-calendar-day cumulative cap + latest-preferred
        # tie-breaker -- extracted to p2p_export.py (nimbus issue #355),
        # see that module's own add_export_bonus_cumulative_caps()
        # docstring for the full "why per-day not global" and "why
        # latest not earliest" reasoning (both real, live household
        # findings, not design choices made in the abstract).
        p2p_export.add_export_bonus_cumulative_caps(
            p, {t: export_bonus[t] for t in range(n)}, periods, grid
        )

    result: LPResult = p.solve()
    if result.status != "optimal":
        return _infeasible_plan(periods, result.status, result.iterations)

    def _get(names: list[str]) -> NDArray[np.float64]:
        return p.values_of(result, names)

    plan_sheddable = [
        SheddableLoadPlan(
            name=sl.name,
            served_kw=sl.forecast_kw - _get(shed_vars[sl.name]),
            shed_kw=_get(shed_vars[sl.name]),
        )
        for sl in sheddable_loads
    ]
    plan_adequacy = [
        AdequacyLoadPlan(
            name=al.name,
            power_kw=(power_arr := _get(adequacy_vars[al.name])),
            delivered_by_deadline_kwh=float(
                np.sum(
                    power_arr[al.earliest_period : al.deadline_period + 1]
                    * hours[al.earliest_period : al.deadline_period + 1]
                )
            ),
        )
        for al in adequacy_loads
    ]

    solar_used_arr = _get(solar_used)
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=_get(charge),
        battery_discharge_kw=_get(discharge),
        battery_soc_kwh=_get(soc),
        grid_import_kw=_get(grid_import),
        grid_export_kw=_get(grid_export),
        export_bonus_kw=_get(export_bonus) if has_export_bonus else np.zeros(n),
        solar_used_kw=solar_used_arr,
        solar_curtailed_kw=solar.forecast_kw - solar_used_arr,
        sheddable_loads=plan_sheddable,
        adequacy_loads=plan_adequacy,
        total_cost=result.objective,
        iterations=result.iterations,
        duals=result.duals,
        reduced_costs=result.reduced_costs,
    )
