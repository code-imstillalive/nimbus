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

from .elements import AdequacyLoadConfig, BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SheddableLoadConfig, SolarConfig
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


def _align_previous_periods(periods: PeriodGrid, previous_plan: Plan | None) -> dict[int, int]:
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
        dev_pos = p.add_variable(f"prox_pos_{family}_{new_idx}", lb=0.0, cost=proximal_weight * hours[new_idx])
        dev_neg = p.add_variable(f"prox_neg_{family}_{new_idx}", lb=0.0, cost=proximal_weight * hours[new_idx])
        p.add_eq_constraint({var_names[new_idx]: 1.0, dev_pos: -1.0, dev_neg: 1.0}, prev_value)


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
        dev_pos = p.add_variable(f"smooth_pos_{family}_{t}", lb=0.0, cost=smoothness_weight * hours[t])
        dev_neg = p.add_variable(f"smooth_neg_{family}_{t}", lb=0.0, cost=smoothness_weight * hours[t])
        p.add_eq_constraint({var_names[t]: 1.0, var_names[t - 1]: -1.0, dev_pos: -1.0, dev_neg: 1.0}, 0.0)


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
    zeros = np.zeros(n)
    return Plan(
        status=status,
        periods=periods,
        battery_charge_kw=zeros,
        battery_discharge_kw=zeros,
        battery_soc_kwh=zeros,
        grid_import_kw=zeros,
        grid_export_kw=zeros,
        export_bonus_kw=zeros,
        solar_used_kw=zeros,
        solar_curtailed_kw=zeros,
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
    for arr, label in ((grid.import_price, "grid.import_price"), (grid.export_price, "grid.export_price")):
        if len(arr) != n:
            msg = f"{label} has {len(arr)} periods, expected {n} (PeriodGrid mismatch)"
            raise ValueError(msg)
    for al in adequacy_loads:
        if al.deadline_period >= n:
            msg = f"Adequacy load '{al.name}': deadline_period ({al.deadline_period}) is outside this PeriodGrid (0..{n - 1})"
            raise ValueError(msg)

    alignment = _align_previous_periods(periods, previous_plan)

    p = LPProblem()

    charge = [p.add_variable(f"battery_charge_{t}", lb=0.0, ub=battery.max_charge_kw) for t in range(n)]
    discharge = [p.add_variable(f"battery_discharge_{t}", lb=0.0, ub=battery.max_discharge_kw) for t in range(n)]
    soc = [p.add_variable(f"battery_soc_{t}", lb=battery.min_soc_kwh, ub=battery.max_soc_kwh) for t in range(n)]
    grid_import = [p.add_variable(f"grid_import_{t}", lb=0.0, ub=grid.import_limit_kw) for t in range(n)]
    # fixed_export_kw (see elements.py's own GridConfig docstring for the
    # full "P2P needs a constant, pre-committed rate, not a price-chased
    # one" finding) -- a period with a real (non-NaN) fixed value gets
    # BOTH lb and ub of its own grid_export[t] variable pinned to exactly
    # that number at construction time, forcing the LP to treat that
    # period's export rate as a given rather than a free decision. Every
    # other period (fixed_export_kw is None, or that period's own entry
    # is NaN) keeps the normal [0, export_limit_kw] bounds, byte-
    # identical to before this field existed.
    grid_export = [
        p.add_variable(
            f"grid_export_{t}",
            lb=(
                float(grid.fixed_export_kw[t])
                if grid.fixed_export_kw is not None and not np.isnan(grid.fixed_export_kw[t])
                else 0.0
            ),
            ub=(
                float(grid.fixed_export_kw[t])
                if grid.fixed_export_kw is not None and not np.isnan(grid.fixed_export_kw[t])
                else grid.export_limit_kw
            ),
        )
        for t in range(n)
    ]

    # Two-tier export bonus (see elements.py's own GridConfig docstring,
    # "export_bonus_price / export_bonus_volume_kwh") -- export_bonus[t]
    # is bounded by grid_export[t] itself just below (added as a real
    # constraint, not a variable upper bound, since grid_export[t] is
    # itself a variable not a constant); the cumulative volume cap is
    # added further below alongside the other whole-horizon constraints.
    has_export_bonus = grid.export_bonus_price is not None and grid.export_bonus_volume_kwh is not None
    export_bonus = (
        [p.add_variable(f"export_bonus_{t}", lb=0.0, ub=grid.export_limit_kw) for t in range(n)]
        if has_export_bonus
        else None
    )

    # Mechanism 3 (confidence-aware dispatch): solar's own EFFECTIVE
    # ceiling for what the LP can count on -- risk_aversion=0.0 or no
    # band present leaves this identical to solar.forecast_kw.
    effective_solar_kw = _risk_adjusted(solar.forecast_kw, solar.lower_kw, solar.upper_kw, risk_aversion, conservative="lower")
    solar_used = [p.add_variable(f"solar_used_{t}", lb=0.0, ub=float(effective_solar_kw[t])) for t in range(n)]

    # Price-risk hedging (see this function's own docstring for the full
    # "afternoons tend to run more expensive than forecast" household
    # finding, and the 2026-08-21 import/export split reasoning) -- each
    # side's own risk_aversion=0.0 or no band present leaves that side
    # identical to grid.import_price/export_price, used below in place of
    # the raw arrays wherever the LP's own cost/revenue is set.
    effective_import_price = _risk_adjusted_one_sided(grid.import_price, grid.import_price_upper, import_price_risk_aversion, direction="up")
    effective_export_price = _risk_adjusted_one_sided(grid.export_price, grid.export_price_lower, export_price_risk_aversion, direction="down")

    # Mechanism 3 continued: sheddable loads' own effective (pessimistic-
    # leaning) demand -- both the shed ceiling and the balance-equation
    # contribution are computed from this, so "how much of this load MUST
    # stay served" scales consistently with whatever the LP is actually
    # planning to serve. Reporting (served_kw/shed_kw below) still uses
    # the RAW forecast, matching solar_curtailed_kw's own treatment.
    shed_vars: dict[str, list[str]] = {}
    effective_shed_forecast: dict[str, NDArray[np.float64]] = {}
    for sl in sheddable_loads:
        eff = _risk_adjusted(sl.forecast_kw, sl.lower_kw, sl.upper_kw, risk_aversion, conservative="upper")
        effective_shed_forecast[sl.name] = eff
        max_shed = [(1.0 - sl.min_fraction) * float(eff[t]) for t in range(n)]
        shed_vars[sl.name] = [p.add_variable(f"shed_{sl.name}_{t}", lb=0.0, ub=max_shed[t]) for t in range(n)]

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
            p.add_variable(f"adequacy_{al.name}_{t}", lb=0.0, ub=al.max_power_kw if al.earliest_period <= t <= al.deadline_period else 0.0)
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
    charge_cost_arr = np.broadcast_to(np.asarray(battery.charge_cost, dtype=np.float64), (n,))
    discharge_cost_arr = np.broadcast_to(np.asarray(battery.discharge_cost, dtype=np.float64), (n,))
    for t in range(n):
        p.set_cost(grid_import[t], effective_import_price[t] * hours[t])
        p.set_cost(grid_export[t], -effective_export_price[t] * hours[t])
        p.set_cost(charge[t], charge_cost_arr[t] * hours[t])
        p.set_cost(discharge[t], discharge_cost_arr[t] * hours[t])
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
            p.set_cost(export_bonus[t], -float(grid.export_bonus_price[t]) * hours[t])
    if battery.terminal_value_breakpoints is not None:
        # Piecewise-linear concave terminal value (2026-08-18, Mark
        # Purcell's audit item #7 -- see BatteryConfig's own docstring
        # for the full "hard corner" problem this replaces, and its own
        # terminal_value_breakpoints docstring for why non-increasing
        # rates make this construction behave concavely with no explicit
        # ordering constraint needed). One small variable per breakpoint
        # (only for the FINAL period, negligible LP cost regardless of
        # horizon length), summing to exactly soc[n-1] - min_soc_kwh --
        # every kWh above the floor priced exactly once, each at its own
        # segment's rate.
        seg_vars = [
            p.add_variable(f"terminal_seg_{i}", lb=0.0, ub=width)
            for i, (width, _rate) in enumerate(battery.terminal_value_breakpoints)
        ]
        p.add_eq_constraint(
            {**{seg: 1.0 for seg in seg_vars}, soc[n - 1]: -1.0},
            -battery.min_soc_kwh,
            name="terminal_value_segments_fill",
        )
        for seg, (_width, rate) in zip(seg_vars, battery.terminal_value_breakpoints, strict=True):
            p.set_cost(seg, -rate)
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
    prev_discharge = previous_plan.battery_discharge_kw if previous_plan is not None else None
    prev_grid_import = previous_plan.grid_import_kw if previous_plan is not None else None
    prev_grid_export = previous_plan.grid_export_kw if previous_plan is not None else None
    for var_names, family, prev_values in (
        (charge, "charge", prev_charge),
        (discharge, "discharge", prev_discharge),
        (grid_import, "grid_import", prev_grid_import),
        (grid_export, "grid_export", prev_grid_export),
    ):
        _add_proximal_penalty(p, var_names, family, alignment, prev_values, hours, proximal_weight)
        if max_rate_kw is not None:
            _add_rate_limit(p, var_names, family, n, alignment, prev_values, max_rate_kw)
        _add_intraplan_smoothness_penalty(p, var_names, family, n, hours, smoothness_weight)

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
        plain_load_total += _risk_adjusted(load.forecast_kw, load.lower_kw, load.upper_kw, risk_aversion, conservative="upper")

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
        p.add_ub_constraint({grid_export[t]: 1.0, solar_used[t]: -1.0, discharge[t]: -1.0}, 0.0)
        # (2) Battery-routed pathway: discharge[t] can only draw on SoC
        # that genuinely existed BEFORE this period's own charging, never
        # energy added within the same period -- discharge[t]*hours[t]/
        # discharge_efficiency <= soc[t-1] - min_soc_kwh (battery.initial_
        # soc_kwh stands in for soc[-1] at t=0, a known constant, so it
        # moves straight to the RHS rather than needing a variable term).
        draw_coeff = hours[t] / battery.discharge_efficiency
        if t == 0:
            p.add_ub_constraint({discharge[t]: draw_coeff}, battery.initial_soc_kwh - battery.min_soc_kwh)
        else:
            p.add_ub_constraint({discharge[t]: draw_coeff, soc[t - 1]: -1.0}, -battery.min_soc_kwh)
        # (3) Two-tier export bonus (see elements.py's own GridConfig
        # docstring): export_bonus[t] can never exceed that SAME period's
        # real total export[t] -- can't claim bonus volume for export
        # that never actually happened -- export_bonus[t] - grid_export[t]
        # <= 0.
        if has_export_bonus:
            p.add_ub_constraint({export_bonus[t]: 1.0, grid_export[t]: -1.0}, 0.0)

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
        starts = periods.period_starts
        # Tie-breaker (2026-08-20, direct household report: "what makes
        # this lightning bolt drop out"). When export_bonus_price is
        # near-flat across a real P2P window -- a genuine, observed
        # pattern, live data showed 0.320 vs 0.314, a ~1.9% gap -- the LP
        # has no real economic preference for WHICH periods claim the
        # capped bonus volume once the total claimed sums to the same cap
        # either way. Confirmed via a local repro
        # (116KAT-HA-AI repo: scratchpad/repro_p2p_bolt_flicker.py) that
        # this genuinely produces an arbitrary, scattered ON/OFF pattern
        # (3 separate transitions on a 24-period test, not a clean split)
        # -- a real degenerate-vertex artifact, not a display bug and not
        # a real economic decision either. grid_export_kw/battery_kw are
        # completely unaffected either way (this only relabels WHICH kWh
        # count toward the bonus cap, never changes how much is exported).
        #
        # Direction flipped LATEST, not earliest (2026-08-20, same day,
        # real live finding, direct household correction: "our window
        # closes 0.00 not 23.50... period"). The original EARLIEST version
        # of this tie-breaker had a real, undesirable side effect once the
        # cap genuinely binds (not just a degenerate tie): confirmed live,
        # a full evening's steady ~14kW discharge exhausted the day's
        # export_bonus_volume_kwh estimate at 23:50, and because the LP
        # had already been nudged to claim every bonus-eligible kWh as
        # EARLY as possible, the plan simply stopped selling at the bonus
        # rate 10 minutes before the real window's own close (00:00) --
        # bonus_price itself was still fully $0.443 through 23:55, only
        # the cap said "no more". LATEST is also the more robust choice,
        # not just the one that matches the real boundary: export_bonus_
        # volume_kwh is a HISTORICAL AVERAGE estimate (p2p_recent_avg_
        # volume_kwh()), not a hard, known-in-advance number -- on any
        # night where the real LocalVolts match volume comes in ABOVE that
        # average, an earliest-claiming plan has already assumed the cap
        # is spent and stops trying, while a latest-claiming plan keeps
        # selling at the bonus rate for as long as the real window (and
        # therefore the real, possibly-higher matched volume) allows.
        # Same underlying mechanism, same epsilon, same "can only break a
        # genuine tie, never override a real price difference" guarantee
        # -- only the ranking direction changed.
        _TIE_BREAK_EPSILON = 1e-7  # $, per day-local rank step
        if starts is None:
            terms = {export_bonus[t]: hours[t] for t in range(n)}
            p.add_ub_constraint(terms, float(grid.export_bonus_volume_kwh), name="export_bonus_cap_global")
            for t in range(n):
                p.set_cost(export_bonus[t], -_TIE_BREAK_EPSILON * (t + 1))
        else:
            by_day: dict[object, list[int]] = {}
            for t, start_t in enumerate(starts):
                by_day.setdefault(start_t.date(), []).append(t)
            # Named per real calendar date (2026-08-18) -- its dual value
            # answers, directly and per-night, "is tonight's P2P bonus
            # volume allotment actually the binding constraint, and how
            # much extra would one more kWh of allotment be worth" --
            # exactly the real question this whole cap exists to model
            # (see this block's own docstring above, "per real calendar
            # day, not once across the whole horizon").
            for day_date, day_indices in by_day.items():
                terms = {export_bonus[t]: hours[t] for t in day_indices}
                p.add_ub_constraint(
                    terms, float(grid.export_bonus_volume_kwh), name=f"export_bonus_cap_{day_date.isoformat()}"
                )
                # Same tie-breaker, scoped to THIS day's own periods only
                # (day-local rank, not a raw global period index) -- keeps
                # the needed dynamic range small regardless of how long
                # the overall horizon is, and naturally resets every day,
                # matching the volume cap's own "resets every real night"
                # philosophy documented just above. LATEST-preferred (see
                # this block's own comment above for why) -- rank+1 grows
                # with t, so the last period in the day gets the most
                # negative (most preferred) cost.
                for rank, t in enumerate(day_indices):
                    p.set_cost(export_bonus[t], -_TIE_BREAK_EPSILON * (rank + 1))

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
                np.sum(power_arr[al.earliest_period : al.deadline_period + 1] * hours[al.earliest_period : al.deadline_period + 1])
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
