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

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from . import p2p_export
from .elements import (
    MIN_CHARGE_DISCHARGE_COST_SPREAD,
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SharedCircuitConfig,
    SheddableLoadConfig,
    SolarConfig,
)
from .lp import LPProblem, LPResult, SolveOptions, SweepRangingStep

_LOGGER = logging.getLogger(__name__)

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

# nimbus issue #613 (Mark Purcell, item 2 of 3 -- item 1, exposing
# shadow_price/plan_shadow_price_forecast, shipped in v0.94.202): the
# "earliest-feasible timing within a materiality band" behaviour change.
# An AdequacyLoadConfig (deferrable load) is free today to satisfy its
# cumulative target_kwh from ANY period in [earliest_period,
# deadline_period] at zero direct cost -- when several periods are
# genuinely tied on real system cost (the exact scenario in #613's own
# report: two solves 4 hours apart both saw the marginal cost of a kWh
# under 0.2c and still deferred, because nothing in the objective valued
# earliness at all), the LP picks whichever tied vertex it happens to
# land on, which can be the LATEST feasible period just as easily as the
# earliest. This is the single-pass earliness-cost-term Mark's own issue
# proposes (the alternative being a two-pass epsilon-constraint re-solve,
# real solver work #494's own offer-curve sweep already shows is
# possible here, but strictly heavier for a plain scheduling preference).
#
# Deliberately NOT `MIN_CHARGE_DISCHARGE_COST_SPREAD` applied directly as
# a flat $/kWh-per-hour-of-delay rate: over a long window (a 24h HWS
# window, or #612's own multi-day repeating windows) that would let the
# cumulative earliness bias grow past 0.01 $/kWh -- the exact threshold
# this codebase everywhere else treats as "definitely a real economic
# difference" -- and start overriding genuine price signals instead of
# only breaking ties among them, the opposite of what "never override a
# genuine economic signal" (DEFAULT_PROXIMAL_WEIGHT_KW's own docstring)
# means. Normalizing by the SOLVE'S OWN horizon length below instead
# fixes the total earliness-driven cost spread, end to end across the
# whole plan, at exactly this one constant regardless of how long any
# individual load's own window is -- by construction it can never be
# mistaken for (or exceed) the smallest real price difference this
# codebase already treats as meaningful, no matter how far out a load's
# own deadline reaches.
DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW: float = MIN_CHARGE_DISCHARGE_COST_SPREAD

# nimbus issue #692 (household, live-observed 2026-09-10): #613's own
# earliness preference was scoped to adequacy_loads (deferrable loads)
# ONLY -- the battery's own charge decision has NEVER had an equivalent
# nudge. Real, direct evidence this is a genuine gap, not a theoretical
# one: a live devhub solve held battery_charge at EXACTLY 0 kW for 27
# minutes (13:23-13:50) at a roughly flat 6.1-8.4c/kWh price band, then
# jumped to near-max charge at 13:55 at 7.62c/kWh -- the IDENTICAL price
# 13:50 held 0kW at. Nothing in the objective distinguished those two
# moments; the LP was free to land on either. Separately, the real
# household's own battery SoC sensor sat at 2-4% (critically low) for
# the entire 11:00-12:00 hour while import price was a moderate,
# perfectly reasonable 6-9c/kWh the whole time, with NOTHING charging
# it -- the exact same "no reason to prefer now over an arbitrary later
# tied moment" degeneracy #613 fixed for loads, now confirmed on the
# battery's own charge variable too.
#
# HALF of MIN_CHARGE_DISCHARGE_COST_SPREAD -- deliberately matching
# DEFAULT_PROXIMAL_WEIGHT_KW/DEFAULT_SMOOTHNESS_WEIGHT_KW's own
# magnitude (0.005), NOT DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW's full
# spread (0.01), and this is a real, verified-not-arbitrary choice, not
# a copy-paste: a battery's own charge variable already carries TWO
# other soft, tiny tie-break costs (proximal_weight, smoothness_weight)
# competing on the exact same variable family -- an adequacy load has
# no such sibling mechanism to share magnitude with, since #613's own
# earliness term is the ONLY soft cost ever applied to adequacy_vars.
# Confirmed live by a real regression: at the full 0.01 spread, this
# term's own per-period gradient OUTWEIGHED smoothness_weight=0.005 in
# test_solver_intraplan_smoothness.py's own flat-price scenario,
# reintroducing the exact jagged charge/discharge burst that mechanism
# exists to eliminate (front-loading charge until the battery's own
# max_soc_kwh ceiling forced a sudden drop to a much slower rate for
# the remainder of the horizon -- a real, hard-ceiling interaction a
# load's own target_kwh delivery, which has no equivalent capacity
# ceiling to slam into, never has to contend with). Halving to match
# smoothness_weight's own magnitude verified this jaggedness drops to
# exactly 0.0 in that same real scenario -- the two soft costs now
# genuinely coexist rather than one silently overpowering the other.
DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW: float = MIN_CHARGE_DISCHARGE_COST_SPREAD / 2

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
    # nimbus issue #484: propagated straight through from the input
    # SheddableLoadConfig's own subentry_id -- see that field's own
    # docstring (elements.py) for why.
    subentry_id: str | None = None


@dataclass(frozen=True)
class AdequacyLoadPlan:
    """One adequacy load's own real result: how much power it was
    actually scheduled to draw each period, the real cumulative energy
    delivered by its own deadline, and (nimbus issue #477) how much of
    `target_kwh` went unmet -- `shortfall_kwh` is 0.0 whenever the
    target was genuinely reachable within `shortfall_price`'s own
    economics (the common case); nonzero means the LP found it cheaper
    to pay `shortfall_price` per kWh than to fully serve the load, which
    is a real, priced decision now (see AdequacyLoadConfig's own
    docstring) rather than the whole plan going infeasible the way it
    used to before #477.

    nimbus issue #482 (price-gated loads, e.g. a Bitcoin miner: "what
    did the miner earn over what its energy was worth"): `profit_
    horizon` is `sum((value_per_kwh[t] - lambda(t)) * power[t] *
    hours[t])` across the whole solve horizon, where lambda(t) is the
    real whole-system marginal cost of a kWh (the same power_balance_
    t{t} dual #613's own shadow_price already exposes). None (not 0.0)
    for a load with no `value_per_kwh` configured at all -- "profit" is
    meaningless without a value to measure it against, same "never
    fabricate a number that isn't real" posture as every other optional
    field here.
    """

    name: str
    power_kw: NDArray[np.float64]
    delivered_by_deadline_kwh: float
    shortfall_kwh: float
    # nimbus issue #484: propagated straight through from the input
    # AdequacyLoadConfig's own subentry_id -- see that field's own
    # docstring (elements.py) for why.
    subentry_id: str | None = None
    # nimbus issue #482 -- see this class's own docstring above.
    profit_horizon: float | None = None


@dataclass(frozen=True)
class GridSignals:
    """Grid-operator signals from HiGHS ranging (nimbus issue #491,
    Signals 2/7 of #489, built on #490's own ranging plumbing) -- real
    numbers a grid operator/aggregator/the household's own telemetry
    can use, distinct from the plan's own dispatch decision itself.

    `grid_import_headroom_kw[t]`/`_kwh`: how much MORE import this
    period could be forced at the SAME marginal price before the plan
    itself would change -- `LPResult.bound_headroom("grid_import_{t}").up`,
    i.e. real ranging, not a guess. Zero (not missing) whenever
    `grid_import_excess_kw[t] > 0` (nimbus issue #390) -- the capped
    `grid_import_{t}` variable is already pinned at its own bound in
    that case, which ranging reports correctly by construction; no
    special-casing needed here. `grid_export_headroom_kw`/`_kwh`
    likewise for export.

    `forced_import_cost[t]`/`forced_export_cost[t]`: what one more kWh
    of forced import/export would cost the plan, $/kWh --
    `reduced_costs["grid_import_{t}"] / hours[t]`. Unlike the headroom
    fields above this does NOT strictly need ranging (reduced costs
    are already computed on every optimal solve) -- gated on
    `ranging_valid` anyway for one uniform, simple contract across this
    whole dataclass (nimbus issue #491's own acceptance criterion:
    "All signals None... when ranging_valid is false"). This IS nimbus
    issue #493's own "shadow_envelope_import_price"/"export" (Signals
    4/7 of #489) once `grid.import_limit_kw`/`export_limit_kw` is a
    real per-period array -- the identical reduced-cost-on-the-bound
    formula #493 itself specifies, already delivered by this field with
    no new computation needed; #493 doesn't get its own separate
    shadow-price field. Sign convention verified directly the same way
    as the plain-scalar-limit case: NEGATIVE when relaxing that period's
    own envelope would be a net benefit (e.g. real curtailed solar
    surplus behind a tight export cap), not positive as an isolated
    reading of #493's own informal note might suggest -- see this
    file's own #491 commit history for the full sign-convention
    reasoning, which applies identically here.

    `flex_available_up_kw`/`down`: the household-level number an
    aggregator could actually call on -- today simply the plan-
    consistent import/export headroom above; nimbus issue #493's own
    "min'd with the envelope" refinement is real, honestly-scoped
    follow-up work, not yet built (there is no envelope to min against
    until #493 lands), so these two fields are a real, useful signal on
    their own already, just not the final envelope-aware version #489's
    own design calls for.

    `load_headroom_up_kwh[t]`/`load_headroom_down_kwh[t]` (nimbus issue
    #492, Signals 3/7 of #489): how much MORE (or less) total load this
    period could genuinely absorb before this period's own real-time
    price λ(t) (the `power_balance_t{t}` row's own dual, already
    published as `shadow_price`) would change -- straight from
    `LPResult.rhs_headroom("power_balance_t{t}")`, the exact same RHS-
    ranging mechanism HAEO #465 calls `range_up` to pick its own
    marginal dual per step. Verified directly: a real 5 kW load against
    a 50 kW import cap with a fully-pinned battery reports `up=45`
    (room to grow before the import cap itself would bind) and
    `down=5` (room to shrink before the balance would need to start
    exporting instead) -- both real, physically-grounded numbers, not
    guesses.

    The whole object is `None` on `Plan.grid_signals` whenever ranging
    wasn't valid for that solve (a non-optimal Plan, or -- not expected
    in practice, since build_plan() always requests ranging -- a HiGHS
    version/edge case where ranging itself came back invalid) --
    unambiguous "not available" for an array-shaped field, the same
    reasoning `duals`/`reduced_costs` already apply via an empty dict,
    adapted for numpy arrays (which can't hold a per-element "unknown"
    the same way a dict entry simply not existing can).
    """

    grid_import_headroom_kw: NDArray[np.float64]
    grid_import_headroom_kwh: NDArray[np.float64]
    grid_export_headroom_kw: NDArray[np.float64]
    grid_export_headroom_kwh: NDArray[np.float64]
    forced_import_cost: NDArray[np.float64]
    forced_export_cost: NDArray[np.float64]
    flex_available_up_kw: NDArray[np.float64]
    flex_available_down_kw: NDArray[np.float64]
    load_headroom_up_kwh: NDArray[np.float64]
    load_headroom_down_kwh: NDArray[np.float64]


@dataclass(frozen=True)
class BatterySignals:
    """One battery participant's own available-headroom signals (nimbus
    issue #491) -- name-matched against the same participant's own
    `BatteryPlan` entry (mirrors that class's own `name`-keyed
    precedent). Two figures per direction, deliberately both published
    side by side rather than picking one:

    `available_up_kw`/`available_down_kw` (PHYSICAL): `max_charge_kw -
    charge[t] + discharge[t]` and the discharge-side mirror -- how much
    this battery's own hardware envelope alone could still move,
    ignoring economics entirely. This is `nem-flex-telemetry`'s own
    existing `assets[].available_up_kw`/`down_kw` convention (#489's
    own research doc) -- always computable, never `None`, needs no
    ranging at all.

    `available_up_ranging_kw`/`available_down_ranging_kw` (PLAN-
    CONSISTENT): from `LPResult.bound_headroom()` on this battery's own
    `battery_charge_{name}_{t}`/`battery_discharge_{name}_{t}`
    variable -- how much more this specific direction the LP would
    actually accept at the SAME marginal price before the optimal
    basis changes. Always `<=` the physical figure (ranging headroom on
    one variable can never exceed that variable's own remaining
    physical bound, since HiGHS caps `col_bound_up` at the variable's
    own ub) -- equal only when nothing about the plan's own economics
    is the binding factor. `None` (the whole array) whenever ranging
    wasn't valid for that solve, same convention as `GridSignals`.
    """

    name: str
    available_up_kw: NDArray[np.float64]
    available_down_kw: NDArray[np.float64]
    available_up_ranging_kw: NDArray[np.float64] | None
    available_down_ranging_kw: NDArray[np.float64] | None


@dataclass(frozen=True)
class LoadSignals:
    """One sheddable or adequacy load's own per-period intent-band
    signal (nimbus issue #492, Signals 3/7 of #489 -- the second of
    that issue's two remaining pieces, alongside its own already-
    shipped switchboard headroom on `GridSignals.load_headroom_up_kwh`/
    `_down_kwh`). For every real period, whether the plan is genuinely
    INDIFFERENT to how much this specific load draws right now
    (`"UNLIMIT"`) or has a real economic reason to cap it at a specific
    figure (`"SET"`).

    Issue #492's own spec cites HAEO's `core/model/intent.py::
    compute_intent()` (HAEO issue #433) as prior art; that file does
    not exist in HAEO's current repo (checked directly via `gh api`
    before writing this, same "verify against the real source" standard
    #696 held itself to) -- this is a direct implementation of #492's
    own written spec, not a literal port of code that could be
    independently checked. Verified directly against #492's own two
    worked examples once building this surfaced a real subtlety the
    spec itself didn't anticipate -- see `_load_signal()`'s own
    docstring in `network.py` for the full finding: `LPResult.bound_
    headroom()`'s own ranging answers "how far could this variable's
    BOUND move before the optimal basis's qualitative STRUCTURE
    changes", not "how far could the value move while the objective
    stays flat" -- those coincide only when the variable is genuinely
    tied (reduced cost == 0). So classification here is driven by
    reduced cost first (the textbook basic/bound-pinned distinction),
    with the ranging band only used to size `UNLIMIT`'s own band; a
    `SET` load reports its own actual committed value as both band
    edges (an honest "limit", not a wider structural range that would
    mislead a reader).

    `intent[t]`: `"UNLIMIT"` when this period's own reduced cost is ~0
    (a genuinely free/basic variable -- indifferent to where in its
    range it sits) or the period's own ceiling is itself ~0 and the
    plan would genuinely take MORE of this load if it could (a
    negative reduced cost against a ~0 upper bound) -- either way,
    nothing meaningful to cap. `"SET"` otherwise.

    `band_min_kw[t]`/`band_max_kw[t]`: on `UNLIMIT`, the real
    structural ranging range (`x[t] -/+ headroom.down/.up` via
    `LPResult.bound_headroom()`, clamped to `[0, ub[t]]`) -- a genuine,
    meaningful "how far this could move" figure for a load the plan
    doesn't care about. On `SET`, both edges collapse to the load's
    own actual committed value -- its honest limit.

    `reduced_cost_per_kwh[t]`: `reduced_costs[var] / hours[t]` -- what
    one more kWh of this load, right now, would cost the plan. ~0 for
    a genuinely free variable (the `UNLIMIT` case); nonzero when
    pinned at a bound by a real cost preference (`SET`) -- same #662
    hours-scaling convention every other per-period $/kWh figure on
    this module already uses.

    `degenerate[t]`: True when `bound_headroom()` itself reported a
    zero-width tie on either side for this period (see `Headroom`'s
    own docstring) -- a real "no headroom right now" answer, not an
    artifact of one solve's own arbitrary tie-break. Independent of
    `intent` -- a `SET` load is typically also degenerate (pinned at a
    bound), but this field reports the ranging's own honest signal
    regardless of which branch produced the band above.

    The whole object is `None` on `Plan.load_signals` (the list itself,
    not a per-entry field) whenever ranging wasn't valid for that
    solve, same convention every other ranging-derived signal on this
    class already uses.
    """

    name: str
    band_min_kw: NDArray[np.float64]
    band_max_kw: NDArray[np.float64]
    reduced_cost_per_kwh: NDArray[np.float64]
    intent: list[str]
    degenerate: NDArray[np.bool_]


@dataclass(frozen=True)
class BatteryPlan:
    """One battery participant's own real result (nimbus issue #467) --
    mirrors AdequacyLoadPlan's own shape/precedent above. Plan's own
    top-level battery_charge_kw/battery_discharge_kw/battery_soc_kwh
    fields stay the SUMMED AGGREGATE across every entry here (zero
    change for any existing reader -- solver_writer.py, dashboard cards,
    quality_report.py/epr.py scoring); this is the per-participant
    breakdown, and what cross-solve stability (proximal/rate-limit,
    matched by `name` against a PREVIOUS Plan's own `batteries` list)
    reads from.
    """

    name: str
    charge_kw: NDArray[np.float64]
    discharge_kw: NDArray[np.float64]
    soc_kwh: NDArray[np.float64]


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
    # nimbus issue #390: the TOTAL real draw (the within-cap portion plus
    # whatever grid_import_excess_kw below had to cover) -- a caller reading
    # this field alone still sees the real, physically-accurate import
    # number even without knowing the excess mechanism exists at all.
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
    # nimbus issue #356 (Mark Purcell): only ever populated (from LPResult's
    # own raw_status) when status == "error" -- HiGHS's own status name
    # (e.g. "Time limit reached"), so a caller can log/report the real
    # solver-level cause instead of treating "not optimal" as an
    # undifferentiated blob. None for every other status, including the
    # genuine "infeasible"/"unbounded" outcomes, which already have their
    # own unambiguous meaning and don't need a raw string to explain them.
    raw_status: str | None = None
    # nimbus issue #390: how much of grid_import_kw above came from the
    # penalized excess-import slack, i.e. real draw the configured
    # `import_limit_kw` couldn't cover on its own. Zero at every period on
    # any normal solve -- a nonzero value here is itself the health signal:
    # the LP had to blow the configured cap for real to keep the plan
    # feasible at all, worth surfacing on a dashboard even when the plan
    # still came back optimal overall. Defaulted (unlike the arrays above)
    # so the handful of tests constructing a Plan directly don't all need
    # updating for a field that's purely diagnostic.
    grid_import_excess_kw: NDArray[np.float64] = field(
        default_factory=lambda: np.zeros(0)
    )
    # 2026-09-07, direct household ask: the risk-aversion sliders
    # (mechanism 3, this module's own docstring) had no visible way to
    # confirm they were doing anything -- "moved slider, nothing
    # happened" is genuinely ambiguous between "the mechanism is a
    # no-op right now because the underlying forecast band is ~zero-width
    # at this moment" and "the slider itself is broken", and nothing on
    # any dashboard could tell the two apart. These three expose the
    # values _risk_adjusted()/_risk_adjusted_one_sided() actually fed
    # the LP, straight from the same locals the LP itself used -- can
    # never drift from what actually ran, unlike a caller re-deriving
    # them separately. Empty (default) on any Plan built before this
    # field existed or constructed directly by a test.
    effective_solar_kw: NDArray[np.float64] = field(default_factory=lambda: np.zeros(0))
    effective_import_price: NDArray[np.float64] = field(
        default_factory=lambda: np.zeros(0)
    )
    effective_export_price: NDArray[np.float64] = field(
        default_factory=lambda: np.zeros(0)
    )
    # nimbus issue #467: per-participant breakdown -- see BatteryPlan's
    # own docstring. Empty (default) on any Plan built before this field
    # existed, constructed directly by a test, or reconstructed from a
    # persisted state file whose own schema predates this field (see
    # solver_writer.py's own load_previous_plan()) -- cross-solve
    # stability simply finds no matching name in that case and treats
    # that battery as if this were the very first solve ever, the same
    # graceful "nothing to align against" fallback _align_previous_
    # periods() already uses.
    batteries: list[BatteryPlan] = field(default_factory=list)
    # nimbus issue #491: see GridSignals'/BatterySignals' own docstrings.
    # None/empty (the default) on any Plan built before these fields
    # existed, constructed directly by a test, non-optimal, or reached
    # from a solve where ranging itself wasn't valid -- same "represent
    # honestly, no fabricated data" posture as every other diagnostic
    # field on this class.
    grid_signals: GridSignals | None = None
    battery_signals: list[BatterySignals] = field(default_factory=list)
    # nimbus issue #492 (Signals 3/7 of #489): see LoadSignals' own
    # docstring. Same empty-default/honest-absence convention as
    # battery_signals above -- one entry per sheddable/adequacy load,
    # empty whenever ranging wasn't valid or no such loads exist.
    load_signals: list[LoadSignals] = field(default_factory=list)
    # nimbus issue #494 (Signals 5/7 of #489): a period-0 demand-response
    # offer ladder -- several (price, kW) steps swept via LPResult.
    # sweep_cost(), not just one (band_min, band_max) at the current
    # price. None (the default) whenever `compute_offer_curve=False`
    # (the common case -- see build_plan()'s own parameter docstring),
    # the plan is non-optimal, or the sweep otherwise wasn't run -- same
    # "represent honestly, no fabricated data" posture as grid_signals
    # above. Each list is sorted ascending by price when populated;
    # `import`'s own kW values are non-increasing in price (a real LP
    # sensitivity property -- raising a variable's own cost coefficient
    # can only keep its optimal value the same or push it down, never
    # up), `export`'s mirror that (non-decreasing, since a HIGHER export
    # price makes exporting more attractive).
    offer_curve_import: list[tuple[float, float]] | None = None
    offer_curve_export: list[tuple[float, float]] | None = None
    # nimbus issue #676: the exact price interval each offer-curve step's
    # own kW value holds for, straight from LPResult.sweep_cost_with_
    # ranging()'s own per-step HiGHS ranging -- not a bracket inferred
    # from neighbouring sample points. Same length and index order as
    # offer_curve_import/export when populated; each entry is
    # (price_lower, price_upper) in real $/kWh (already converted from
    # the LP's own raw per-period cost-coefficient units the same way
    # nimbus issue #662 established for the plain per-period dual), or
    # `None` for a step where ranging itself came back invalid. `None`
    # (the whole list, not per-entry) whenever offer_curve_import/export
    # itself is `None` -- same "represent honestly, no fabricated data"
    # posture as every other diagnostic field on this class.
    offer_curve_import_ranging: list[tuple[float, float] | None] | None = None
    offer_curve_export_ranging: list[tuple[float, float] | None] | None = None
    # How long the sweep itself took (nimbus issue #494's own "total sweep
    # time logged and under 0.5s on the reference grid" acceptance
    # criterion) -- same None-when-not-computed convention as the two
    # curve fields above.
    offer_curve_sweep_seconds: float | None = None

    @property
    def is_optimal(self) -> bool:
        return self.status == "optimal"

    @property
    def import_cap_breach_kwh(self) -> float:
        """Total real energy this plan drew above `import_limit_kw`, summed
        across the whole horizon. 0.0 whenever grid_import_excess_kw is
        empty (a Plan built before this field existed) or all-zero (the
        normal case)."""
        if self.grid_import_excess_kw.size == 0:
            return 0.0
        return float(np.sum(self.grid_import_excess_kw * self.periods.hours))


def _align_previous_periods(
    periods: PeriodGrid, previous_plan: Plan | None
) -> dict[int, int]:
    """Map new-grid period index -> previous_plan period index: the OLD
    period whose own real [start, start+hours) interval CONTAINS the new
    period's start time (within _ALIGNMENT_TOLERANCE at either edge).
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

    nimbus issue #635 (Mark Purcell, real repro captured from two
    consecutive live plans): this used to require the new period's own
    start to match an old period's start EXACTLY (within tolerance) --
    correct whenever both grids share the same period width, but a
    mid-slot solve (triggered by a second price source updating ~1
    minute after the phase-locked cron -- see #633) builds tier-0
    1-minute periods (e.g. 17:26, 17:27, 17:28, 17:29) that don't start
    at any boundary the previous, cron-triggered plan's own 5-minute
    grid (17:25, 17:30, ...) ever used. Those periods got NO alignment
    entry at all -- proximal regularization (the mechanism that tethers
    a new solve to what the previous one committed to) was silently OFF
    for exactly them, and with the evening's near-zero economic
    difference between exporting a little or a lot, the LP was free to
    park an arbitrary vertex at the hard power limit for that one
    isolated minute, which publish_plan() then published as the live
    setpoint. Real captured evidence: four such untethered 1-minute
    periods read 25.0 kW (the fleet's own export limit) while the
    previous plan's overlapping 5-minute period had called for 17.9 kW.

    Fix: a new period aligns to whichever old period's own real time
    interval it falls inside, not just an old period starting at the
    exact same instant -- a 1-minute period at 17:26 now correctly
    tethers to the old plan's 17:25-17:30 period's own value, the same
    stabilizing pressure every boundary-aligned period already gets.
    When both grids share the same period width (the common case, no
    mid-slot solve involved), interval-containment and exact-start-match
    are the same test by construction (every period start is itself an
    interval boundary) -- this is a strict generalization, not a
    behavior change, for that case.

    nimbus issue #356 (Mark Purcell), item 4: this is still the same
    O(n+m) two-pointer merge introduced then, not a regression back to
    the original O(n*m) nested loop -- both `new_starts` and `old_starts`
    are guaranteed monotonically increasing (PeriodGrid rejects any
    non-positive period duration, and `period_starts` is a running
    cumulative sum), so the OLD index whose interval could contain a
    LATER new period can never be earlier than the old index that
    contained an earlier new period -- `old_idx` only ever advances past
    an old period once its own END has passed the new period's start.
    """
    if previous_plan is None or not previous_plan.is_optimal:
        return {}
    new_starts = periods.period_starts
    old_starts = previous_plan.periods.period_starts
    if new_starts is None or old_starts is None:
        return {}
    old_hours = previous_plan.periods.hours
    mapping: dict[int, int] = {}
    old_idx = 0
    n_old = len(old_starts)

    def _old_end(idx: int) -> datetime:
        return old_starts[idx] + timedelta(hours=float(old_hours[idx]))

    for new_idx, new_t in enumerate(new_starts):
        while old_idx < n_old - 1 and _old_end(old_idx) <= new_t + _ALIGNMENT_TOLERANCE:
            old_idx += 1
        if (
            old_starts[old_idx] - _ALIGNMENT_TOLERANCE
            <= new_t
            < _old_end(old_idx) + _ALIGNMENT_TOLERANCE
        ):
            mapping[new_idx] = old_idx
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
    *,
    use_secondary: bool = False,
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

    `use_secondary` (nimbus issue #696, Stage 2): when True, this
    penalty's own cost is added to the LPProblem's SECONDARY channel
    (`set_secondary_cost`) instead of directly at variable-construction
    time (`add_variable(cost=...)`, the primary channel) -- see
    `build_plan()`'s own docstring for exactly when this is set (never
    on a solve that will end up a MIP, since the primary/secondary
    solve architecture doesn't support that combination yet).
    """
    if proximal_weight <= 0.0 or not alignment or previous_values is None:
        return
    for new_idx, old_idx in alignment.items():
        prev_value = float(previous_values[old_idx])
        penalty_cost = proximal_weight * hours[new_idx]
        dev_pos = p.add_variable(
            f"prox_pos_{family}_{new_idx}",
            lb=0.0,
            cost=0.0 if use_secondary else penalty_cost,
        )
        dev_neg = p.add_variable(
            f"prox_neg_{family}_{new_idx}",
            lb=0.0,
            cost=0.0 if use_secondary else penalty_cost,
        )
        if use_secondary:
            p.set_secondary_cost(dev_pos, penalty_cost)
            p.set_secondary_cost(dev_neg, penalty_cost)
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
    *,
    use_secondary: bool = False,
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

    `use_secondary` (nimbus issue #696, Stage 2): same meaning as
    `_add_proximal_penalty()`'s own parameter -- routes this penalty's
    cost to the LPProblem's secondary channel instead of primary.
    """
    if smoothness_weight <= 0.0:
        return
    for t in range(1, n):
        penalty_cost = smoothness_weight * hours[t]
        dev_pos = p.add_variable(
            f"smooth_pos_{family}_{t}",
            lb=0.0,
            cost=0.0 if use_secondary else penalty_cost,
        )
        dev_neg = p.add_variable(
            f"smooth_neg_{family}_{t}",
            lb=0.0,
            cost=0.0 if use_secondary else penalty_cost,
        )
        if use_secondary:
            p.set_secondary_cost(dev_pos, penalty_cost)
            p.set_secondary_cost(dev_neg, penalty_cost)
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


def _infeasible_plan(
    periods: PeriodGrid,
    status: str,
    iterations: int,
    raw_status: str | None = None,
) -> Plan:
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
        raw_status=raw_status,
        grid_import_excess_kw=np.zeros(n),
    )


# Real AEMO NEM price limits, not synthetic stress bounds -- confirmed by
# Mark Purcell directly (nimbus issue #675) after an earlier session
# wrongly assumed these were arbitrary. -1.00 = the Market Floor Price
# (-$1,000/MWh), independently confirmed current. 23.20 = the Market
# Price Cap (nimbus issue #705, resolving #675's own long-open "may
# already be a step behind the real current figure" question) --
# confirmed directly by Mark Purcell, 2026-09-10: $23.20/kWh
# ($23,200/MWh), the real current AEMC-determined MPC, correcting this
# constant's previous $20.00 placeholder value (never independently
# verified against a primary AEMO source, per #675's own text).
_OFFER_CURVE_DOMAIN_MIN: float = -1.00
_OFFER_CURVE_DOMAIN_MAX: float = 23.20

# Real live data (2026-09-10, this household) shows 2-3 genuine segments
# per side; this is a generous multiple of that, not a tuned minimum --
# see _offer_curve_ranging_walk()'s own docstring for what happens if a
# real curve ever needs more (the walk stops honestly, it doesn't guess).
_OFFER_CURVE_MAX_BREAKPOINTS: int = 8

# Relative step past a discovered breakpoint before the next solve --
# same magnitude/reasoning as `lp.py`'s own HAEO-ported LexOptions
# phase-3 epsilon (issue #696/#699, `epsilon = max(1e-6, abs(
# secondary_value) * 1e-6)`): small enough to stay below any real
# tariff's own granularity, large enough to clear float noise at the
# boundary itself.
_OFFER_CURVE_NUDGE_REL: float = 1e-6

# nimbus issue #705: minimum real price gap (in $/kWh) worth a dedicated
# extra "backstop" solve -- see _offer_curve_ranging_walk()'s own
# backstop paragraph. Below this, a gap between the walk's own last
# resolved boundary and the retail anchor's own ranging is float noise /
# a genuinely negligible sliver, not a real unresolved region -- no
# tariff this project has ever seen prices this close together
# (0.01c/kWh) as two economically distinct decision points.
_OFFER_CURVE_BACKSTOP_MIN_GAP: float = 1e-4


def _offer_curve_ranging_walk(
    result: LPResult,
    var: str,
    *,
    start: float = _OFFER_CURVE_DOMAIN_MIN,
    ascending: bool = True,
    retail: float,
    hours0: float,
    negated: bool,
) -> tuple[list[tuple[float, float]], list[tuple[float, float] | None]]:
    """Nimbus issue #678: walks this variable's own real piecewise-
    constant breakpoints directly via repeated `LPResult.sweep_cost_
    with_ranging()` calls, instead of sampling #494's original fixed
    7-point grid (`_offer_curve_price_grid()`, removed by this issue).

    Each step's own `cost_up` (ascending) or `cost_dn` (descending) --
    the EXACT price at which its plateau stops being optimal, straight
    from HiGHS's own sensitivity analysis, not a guess -- becomes the
    next step's own starting price, nudged forward/backward by
    `_OFFER_CURVE_NUDGE_REL` so the next solve lands unambiguously on
    the NEXT plateau rather than re-landing on the same point. This
    nudge answers #678's own explicitly-flagged open question ("whether
    exactly at the breakpoint price needs a small epsilon nudge") --
    yes: the boundary price itself is a genuine LP tie between the two
    adjacent plateaus (confirmed by inspecting real ranging output where
    a segment's own reported interval touches its neighbour's at exactly
    one shared price), so solving precisely AT it can return either
    optimal vertex depending on solver internals, not reliably the new
    one.

    `start`/`ascending` (nimbus issue #705): which end of the domain the
    walk explores first, and which direction it moves. Import still
    walks `ascending=True` from `_OFFER_CURVE_DOMAIN_MIN` (the real AEMO
    Market Floor Price) upward -- the default, unchanged from #678's own
    original behaviour, every existing caller of this function
    unaffected. Export instead walks `ascending=False` from
    `_OFFER_CURVE_DOMAIN_MAX` (the Market Price Cap) downward -- real
    live data confirms the two curves' own genuinely interesting
    structure sits at OPPOSITE ends of the domain: import's real
    breakpoints cluster near the floor (a household stops buying as
    price rises), export's cluster near the cap (a household sells more
    as price rises) -- see this function's own module-level docstring
    reference and #705's own issue text for the live household evidence.
    Walking export from the floor upward (#678's original, uniform
    choice) spent its entire iteration budget on the near-zero region
    every time, the ECONOMICALLY LEAST interesting part of an export
    curve, and never reached the high-price region where a household's
    real willingness to sell actually changes. Reversing direction for
    export costs nothing extra (still capped at
    `_OFFER_CURVE_MAX_BREAKPOINTS` steps) and explores the side that
    actually matters.

    The walk stops the moment any of these hold, each an honest "nothing
    more to find here," never a guess:
    - the current plateau's own ranging is unbounded in the walk's own
      direction of travel (`cost_up >= _OFFER_CURVE_DOMAIN_MAX` ascending,
      `cost_dn <= _OFFER_CURVE_DOMAIN_MIN` descending) -- it already
      provably holds through the far end of the domain, so no further
      step could add real information;
    - ranging itself came back invalid at this step (a genuine
      degenerate basis -- #678's own flagged safety concern: stop rather
      than guess how to continue);
    - the next candidate breakpoint is not strictly past the previous
      one in the walk's own direction of travel (a non-monotonic/
      degenerate ranging result, which real LP parametric-sensitivity
      theory says should never happen for a well-posed single-variable
      sweep -- treated as a hard stop, not something to paper over);
    - `_OFFER_CURVE_MAX_BREAKPOINTS` real walk steps have already run
      (the explicit iteration cap #678 asked for).

    `retail` (period 0's own effective price) is ALWAYS separately swept
    on top of the walk, regardless of where it falls relative to the
    walked breakpoints -- #494's own "curve at retail equals the main
    plan's period-0 dispatch" acceptance check needs a real solve at
    that exact price, not a value inferred from whichever segment
    contains it. A duplicate solve on the rare cycle where retail
    happens to exactly coincide with an already-walked price is
    harmless (identical cost coefficient, identical result) and not
    worth special-casing away.

    **Backstop (nimbus issue #705):** starting the walk at one end of the
    domain means its own iteration budget can be entirely consumed by
    dense structure near THAT end, leaving a real, genuinely unexplored
    gap between the walk's own last resolved boundary and the retail
    anchor -- confirmed live, 2026-09-10, on this exact household's real
    data: export's own 6.41c-9.21c band hid a real 5.48kW->11.37kW jump
    that neither that cycle's walk (spent entirely below 6.41c) nor the
    retail solve itself (at 9.22c) ever touched. After the walk and the
    retail solve both complete, if the walk resolved at least one real
    boundary (`start` itself wasn't already the whole story) AND retail's
    own ranging doesn't already reach back to meet it, exactly ONE extra
    solve is run at the midpoint of that gap -- closing the specific
    known blind spot for a bounded, worthwhile extra cost (never more
    than one solve per curve), not a general redesign of the walk's own
    adaptive discovery elsewhere, which this experiment (comparing
    against several fixed manual sweep grids on real live data) confirmed
    nothing beats. A gap narrower than `_OFFER_CURVE_BACKSTOP_MIN_GAP` is
    treated as already effectively covered -- no backstop solve wasted on
    float noise.

    Supersedes #675's own "add the real forecast's own min/max as extra
    sweep points" ask, per that issue's own text: a walk that finds
    every real breakpoint exactly needs no extra landmark points to
    guess where the interesting prices are -- it already covers the
    whole domain with genuine precision, not just wherever a sample
    happened to land. #675's separate "$20 cap may be stale" question is
    now resolved -- nimbus issue #705 confirmed and corrected the real
    Market Price Cap (see the domain constants' own comment above).
    """
    prices: list[float] = []
    values: list[float] = []
    intervals: list[tuple[float, float] | None] = []

    def solve_at(price: float) -> tuple[float, float] | None:
        cost = price * hours0 if not negated else -price * hours0
        (step,) = result.sweep_cost_with_ranging(var, [cost])
        interval = _offer_curve_price_interval(step, hours0, negated=negated)
        prices.append(price)
        values.append(step.value)
        intervals.append(interval)
        return interval

    domain_limit = _OFFER_CURVE_DOMAIN_MAX if ascending else _OFFER_CURVE_DOMAIN_MIN
    price = start
    last_bound: float | None = None
    for _ in range(_OFFER_CURVE_MAX_BREAKPOINTS):
        interval = solve_at(price)
        if interval is None:
            break
        lower, upper = interval
        bound = upper if ascending else lower
        reached_domain_limit = (
            bound >= domain_limit if ascending else bound <= domain_limit
        )
        if reached_domain_limit:
            break
        if last_bound is not None:
            stalled = (
                bound <= last_bound + 1e-12
                if ascending
                else bound >= last_bound - 1e-12
            )
            if stalled:
                break
        last_bound = bound
        nudge = max(_OFFER_CURVE_NUDGE_REL, abs(bound) * _OFFER_CURVE_NUDGE_REL)
        price = bound + nudge if ascending else bound - nudge

    solve_at(retail)

    # nimbus issue #705: one gap-targeted backstop solve -- see this
    # function's own docstring paragraph above for the full reasoning.
    retail_interval = intervals[-1]
    if last_bound is not None and retail_interval is not None:
        retail_lower, retail_upper = retail_interval
        gap_edge = retail_lower if ascending else retail_upper
        gap = (gap_edge - last_bound) if ascending else (last_bound - gap_edge)
        if gap > _OFFER_CURVE_BACKSTOP_MIN_GAP:
            solve_at((last_bound + gap_edge) / 2.0)

    order = sorted(range(len(prices)), key=lambda i: prices[i])
    curve = [(prices[i], values[i]) for i in order]
    ranging = [intervals[i] for i in order]
    return curve, ranging


def _offer_curve_price_interval(
    step: SweepRangingStep, hours0: float, *, negated: bool
) -> tuple[float, float] | None:
    """Converts one `sweep_cost_with_ranging()` step's own `cost_dn`/
    `cost_up` (raw LP cost-coefficient units) into a real (price_lower,
    price_upper) $/kWh interval (nimbus issue #676) -- `None` when either
    bound came back invalid at this step.

    Two conversions, not one, mirroring exactly how the sweep's own
    `costs` were built:

    1. `÷ hours0` -- the swept cost coefficient is `price * hours[0]`
       (see the sweep call sites below), the identical period-scaling
       nimbus issue #662 already established needs undoing to recover a
       true $/kWh figure from a raw LP cost/dual value.
    2. Sign, for export only -- the export sweep negates its own cost
       coefficient (`-price * hours[0]`, since export EARNS revenue; see
       the export sweep call's own comment), so `cost = -price * hours0`
       is a DECREASING function of price. That flips which ranging bound
       is the lower vs. upper real price: `cost_dn` (the cost
       coefficient's own lower bound) corresponds to the HIGHER real
       price, and `cost_up` to the LOWER one. Getting this backwards
       would silently swap a real "cheaper below this" reading into
       "cheaper above this" -- worth being this explicit about it.
    """
    if step.cost_dn is None or step.cost_up is None:
        return None
    if negated:
        price_lower = -step.cost_up.value / hours0
        price_upper = -step.cost_dn.value / hours0
    else:
        price_lower = step.cost_dn.value / hours0
        price_upper = step.cost_up.value / hours0
    return (price_lower, price_upper)


def build_plan(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    batteries: list[BatteryConfig],
    solar: SolarConfig,
    loads: list[LoadConfig] | None = None,
    sheddable_loads: list[SheddableLoadConfig] | None = None,
    adequacy_loads: list[AdequacyLoadConfig] | None = None,
    shared_circuits: list[SharedCircuitConfig] | None = None,
    previous_plan: Plan | None = None,
    proximal_weight: float = DEFAULT_PROXIMAL_WEIGHT_KW,
    max_rate_kw: float | None = None,
    smoothness_weight: float = 0.0,
    risk_aversion: float = 0.0,
    import_price_risk_aversion: float = 0.0,
    export_price_risk_aversion: float = 0.0,
    soft_soc_penalty_per_kwh: float | None = None,
    compute_signals: bool = False,
    compute_offer_curve: bool = False,
    adequacy_earliness_budget_kw: float = DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW,
    adequacy_semi_continuous: bool = True,
    battery_charge_earliness_budget_kw: float = DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
    solve_options: SolveOptions | None = None,
) -> Plan:
    """Build and solve one LP for the given horizon/inputs. Pure function --
    no I/O, no HA dependency, safe to call from anywhere including a plain
    local test script.

    `previous_plan`/`proximal_weight`/`max_rate_kw`/`risk_aversion` are
    the three cross-solve stability mechanisms -- see this module's own
    docstring for the full design. All default to "off" (a bare, single-
    solve LP, unchanged from before these existed).

    `adequacy_earliness_budget_kw` (nimbus issue #613) is ON by default,
    unlike the mechanisms above -- see DEFAULT_ADEQUACY_EARLINESS_
    BUDGET_KW's own docstring. Set to 0.0 to fully disable (every
    adequacy load reverts to today's "zero direct cost anywhere in its
    own window" behaviour).

    `battery_charge_earliness_budget_kw` (nimbus issue #692) is ON by
    default, mirroring `adequacy_earliness_budget_kw` above but applied
    to every battery's own charge variable instead of a load's -- see
    DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW's own docstring for the
    real, live evidence this fixes. Set to 0.0 to fully disable.

    `adequacy_semi_continuous` (nimbus issue #616) is also ON by
    default -- see the semi-continuous/single-block constraint block's
    own comment, right after adequacy_vars is built, for the full
    reasoning. Set to False to revert every adequacy load to a
    continuous relaxation (free to deliver any fractional power level
    anywhere in its own window) -- this turns the whole problem back
    into a pure LP (no binary variables), so it's also the fallback a
    caller with a genuine performance concern on a very large horizon
    can reach for.

    `shared_circuits` (SharedCircuitConfig, see its own docstring): caps
    the COMBINED power of two or more `adequacy_loads` sharing one real
    circuit's headroom, at every period -- e.g. two hot-water heaters
    that must never draw simultaneously. Each `member_names` entry must
    match a `name` already present in `adequacy_loads`; a mismatch raises
    ValueError immediately rather than silently doing nothing. `None`
    (the default) is a complete no-op -- every adequacy load keeps its
    own existing, independent freedom to run anywhere in its own window.

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
    a battery's own `initial_soc_kwh` may legitimately arrive below its
    own min_soc_kwh (a template-averaged SoC sensor, a cold pack, a
    fresh install starting empty, sensor drift) or, in principle, above
    max_soc_kwh. `soc[t]` itself is only ever hard-bounded to the true
    physical range `[0, capacity_kwh]`; going outside `[min_soc_kwh,
    max_soc_kwh]` costs a real penalty (this parameter, per kWh per
    hour) instead of being impossible. `None` (the default) auto-derives
    the penalty from the real $/kWh signals already in this call -- see
    DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER's own comment for why it takes
    the max across import price, export price, AND any configured
    terminal_value_breakpoints rate across EVERY battery (not import
    price alone, and not just the first battery in `batteries`). A
    caller that already knows a good value (e.g. a real historical peak
    import price across a longer window than this one solve sees) can
    pass it explicitly instead.

    When every battery's own `initial_soc_kwh` starts inside its own
    `[min_soc_kwh, max_soc_kwh]` and stays there for the whole horizon,
    this mechanism is a complete no-op -- the penalty terms all evaluate
    to exactly zero and the plan is numerically identical to the version
    of this function that hard-bounded `soc[t]` directly. It only ever
    engages for a genuinely below-floor (or above-ceiling) starting/
    drifting state, in which case the LP schedules real recovery
    (charging at cheap import windows, waiting through export windows)
    using whatever real price/solar/load context this solve actually
    has, rather than either crashing (the pre-#325 behaviour) or
    silently reporting a fictional in-range starting SoC (the #325/#327
    clamp-and-pretend behaviour this mechanism replaces).

    `batteries` (nimbus issue #467, Mark Purcell's own spec, stage 1 --
    "just multi-battery build_plan() support"): a list of one or more
    independently-metered battery participants (NOT a list of physical
    packs behind one inverter -- see BatteryConfig's own docstring,
    "topology" note, for why that's still correctly a single aggregate
    entry). Each gets its own LP variable families (keyed by its own
    `name`, matching the existing `adequacy_vars`-style name-keyed
    pattern), its own independent SoC recursion/terminal-value/wash-
    trade-cap treatment, and its own entry in the output `Plan.batteries`
    list. `Plan.battery_charge_kw`/`battery_discharge_kw`/`battery_
    soc_kwh` stay the SUMMED AGGREGATE across every battery here -- every
    existing reader of those three fields needs zero changes. The
    P2P `fixed_export_kw` charge gate (see `charge`'s own construction
    below) applies to `batteries[0]` only -- an explicit, open household
    decision (Mark's own suggestion, not yet ruled on) for which battery
    is the one actually feeding the committed export window; every other
    battery in the list charges/discharges under its own plain max_
    charge_kw/max_discharge_kw bounds with no P2P-window gating.

    `compute_signals` (nimbus issue #491, Signals 2/7 of #489): opt-in
    (default `False`) -- when `True`, requests HiGHS ranging on this
    solve (`LPProblem.solve(ranging=True)`, #490) and populates `Plan.
    grid_signals`/the ranging-derived half of `Plan.battery_signals`
    from it. Deliberately NOT on by default: confirmed live via this
    project's own real-scale timing regression test that ranging's own
    cost is NOT uniformly "cheap, ~0.03s" the way a bare LP solve is at
    this project's production scale -- a 288-period battery-power-curve
    scenario (piecewise segment variables, this project's own most
    LP-structurally-complex real shape) measured ranging adding ~5s on
    top of a ~0.6s bare solve, nowhere near the "cheap post-solve pass"
    #490's own module docstring describes for a plain LP. Every existing
    caller (every test/caller predating this parameter) is completely
    unaffected either way -- `Plan.battery_signals`' own PHYSICAL fields
    (`available_up_kw`/`available_down_kw`, pure arithmetic on the
    solved dispatch, no ranging needed) are still always populated
    regardless of this flag; only the four ranging-derived fields
    (`grid_signals` as a whole, and each `BatterySignals`'s own
    `available_up_ranging_kw`/`available_down_ranging_kw`) are gated on
    it. A caller that wants the real signals (e.g. a future dashboard
    or telemetry feed, not yet built) opts in explicitly, and should
    re-measure its own real timing budget at ITS OWN real problem scale
    before doing so, rather than trust this docstring's own single
    measured data point as universal.

    `compute_offer_curve` (nimbus issue #494, Signals 5/7 of #489):
    opt-in (default `False`) -- when `True`, requests `LPProblem.solve(
    keep_basis=True)` and, on an optimal result, walks period 0's
    `grid_import`/`grid_export` cost coefficients directly along their
    own real breakpoints (`_offer_curve_ranging_walk()`, nimbus issue
    #678) via repeated `LPResult.sweep_cost_with_ranging()` calls,
    populating `Plan.offer_curve_import`/`offer_curve_export` -- a real
    (price, kW) demand-response bid ladder for period 0, not just one
    (band_min, band_max) at the CURRENT price the way `compute_signals`
    above already gives. Each walk step is a warm-started re-solve from
    period 0's own already-loaded optimal basis (milliseconds, not a
    fresh cold solve); the walk finds every real segment between the
    AEMO Market Floor Price and Market Price Cap exactly, in as many
    re-solves as the curve actually has (typically 2-4 per side on real
    data, capped at `_OFFER_CURVE_MAX_BREAKPOINTS`), rather than sampling
    a fixed grid and hoping a sample point lands near the interesting
    price -- #494's original fixed 7-point grid (`_offer_curve_price_
    grid()`) is removed, superseded by this exact-breakpoint walk. Stays
    opt-in, matching #494's own explicit cadence requirement ("not every
    5-minute solve by default") and this module's own established "extra
    solver capability is opt-in" convention. Every existing caller is
    unaffected either way.

    Nimbus issue #705: import walks from the floor upward, export from
    the cap downward (each curve's own real breakpoints cluster near
    ITS OWN economically interesting end, confirmed on live household
    data), plus one gap-targeted backstop solve per curve when the walk
    and the mandatory retail solve leave a real gap between them -- see
    `_offer_curve_ranging_walk()`'s own docstring for the full mechanism
    and the live evidence motivating both changes.

    Also populates `Plan.offer_curve_import_ranging`/`offer_curve_export_
    ranging` (nimbus issue #676) -- each walked step's own EXACT real
    $/kWh price interval, straight from HiGHS's own per-step cost
    ranging (the SAME ranging call that finds each next breakpoint in
    the first place, so this carries no additional cost beyond the walk
    itself, unlike `compute_signals`'s own separate ranging pass above).

    `solve_options` (nimbus issue #696, Stage 2): `None` (the default)
    preserves this function's own pre-#696 behavior EXACTLY -- every
    tie-break mechanism above (`proximal_weight`, `smoothness_weight`,
    `adequacy_earliness_budget_kw`, `battery_charge_earliness_budget_kw`)
    stays a hand-tuned magnitude summed directly into the one real cost
    objective, a single ordinary blended solve, byte-identical to every
    existing caller/test. Pass `lp.CalibratedOptions()` (or `LexOptions`/
    `BlendedOptions`) to opt a specific solve into the real primary/
    secondary architecture instead: all four mechanisms above move to
    the LPProblem's SECONDARY channel, at their own SAME existing
    magnitudes (their relative proportions to each other are preserved
    exactly; only the overall scale is what `CalibratedOptions` searches
    for safely, replacing manual verification with a real, per-solve
    safety search).

    nimbus issue #702 (#696's own tracked MIP follow-up, now closed):
    this architecture now DOES support a MIP (`adequacy_loads` non-empty
    AND `adequacy_semi_continuous=True`, the default -- see that
    parameter's own docstring). `LPProblem.solve(options=...)` pins the
    real binary assignment (solved against primary alone, via
    `lp.py`'s own `_pin_binaries_to_mip_optimum()`) before the
    lex/blended/calibrated machinery ever runs, so the four mechanisms
    below tie-break the CONTINUOUS variables exactly as they do on a
    pure-LP solve, without ever second-guessing which adequacy loads
    the MIP itself chose to run. See `LPProblem.solve()`'s own
    docstring for the full mechanism.
    """
    loads = loads or []
    sheddable_loads = sheddable_loads or []
    adequacy_loads = adequacy_loads or []
    shared_circuits = shared_circuits or []
    n = periods.n_periods
    hours = periods.hours

    # nimbus issue #696/#702: determined from CONFIG alone, before any
    # variable is registered. Computed once, up front, because it must
    # be internally consistent for the WHOLE function -- the four
    # tie-break mechanisms below apply their own cost at several
    # different points, and the final p.solve() call happens only at
    # the very end; deciding this from live LPProblem.is_mip state at
    # each of those points would risk a mechanism applied to one
    # channel while a LATER binary registration (adequacy_on_*, still
    # to come) silently changes which channel the final solve actually
    # reads from. #702 removed the MIP exclusion this used to carry
    # (`lp.py` now pins the binary assignment before ever reading
    # secondary costs, so a MIP solve is exactly as safe to route
    # through the secondary channel as a pure LP one).
    _use_secondary_costs = solve_options is not None

    # nimbus issue #493 (Signals 4/7 of #489): grid.import_limit_kw/
    # export_limit_kw may now be a plain scalar (every existing caller)
    # or a real per-period array (a DNSP's own dynamic operating
    # envelope) -- resolved to a real per-period array exactly once
    # here, same np.broadcast_to() convention BatteryConfig's own
    # charge_cost/discharge_cost array-or-scalar fields already use.
    # Every call site below indexes this array by t instead of reading
    # grid.import_limit_kw/export_limit_kw directly.
    import_limit_arr = np.broadcast_to(
        np.asarray(grid.import_limit_kw, dtype=np.float64), (n,)
    )
    export_limit_arr = np.broadcast_to(
        np.asarray(grid.export_limit_kw, dtype=np.float64), (n,)
    )

    # nimbus issue #467: at least one battery is required -- an empty
    # list has no real meaning for this LP (every wash-trade/power-
    # balance construction below assumes a real dispatchable participant
    # exists), and a caller passing [] almost certainly meant "battery
    # physically disabled" (max_charge_kw=max_discharge_kw=0.0 on a real
    # BatteryConfig, same as every existing test that models that case),
    # not "no battery at all".
    if not batteries:
        msg = "build_plan() requires at least one BatteryConfig in `batteries`"
        raise ValueError(msg)
    _battery_names = [b.name for b in batteries]
    if len(set(_battery_names)) != len(_battery_names):
        msg = f"`batteries` entries must have unique names, got {_battery_names}"
        raise ValueError(msg)

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
        # nimbus issue #612: same bounds check, per window -- elements.py
        # can't check this at construction time either (no PeriodGrid to
        # check against there, same reasoning as deadline_period above).
        if al.windows is not None:
            for i, w in enumerate(al.windows):
                if w.deadline_period >= n:
                    msg = (
                        f"Adequacy load '{al.name}': windows[{i}].deadline_period "
                        f"({w.deadline_period}) is outside this PeriodGrid (0..{n - 1})"
                    )
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
    for b in batteries:
        if b.terminal_value_period_indices is not None:
            for idx in b.terminal_value_period_indices:
                if idx >= n:
                    msg = f"BatteryConfig '{b.name}'.terminal_value_period_indices: index {idx} is outside this PeriodGrid (0..{n - 1})"
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
        for b in batteries:
            if b.terminal_value_breakpoints is not None:
                candidate_rates.append(
                    max(rate for _width, rate in b.terminal_value_breakpoints)
                )
        soft_soc_penalty_per_kwh = DEFAULT_SOFT_SOC_PENALTY_MULTIPLIER * max(
            *candidate_rates, 0.01
        )

    p = LPProblem()

    # nimbus issue #694: whether the household's price-spike override
    # (#567) is genuinely active for THIS solve's period 0 -- computed
    # once, up front, because both the per-battery discharge/charge pin
    # below AND grid_export[0]'s own bounds (further down) need the same
    # fact. Mirrors the per-iteration `spike_active_now` the batteries
    # loop below computes for b_idx==0 exactly (only ever batteries[0],
    # only ever period 0, see BatteryConfig.spike_override_discharge_kw's
    # own docstring) -- kept as a single upfront bool rather than reached
    # for out of the loop's own per-iteration variable, since grid_export
    # construction happens in a separate loop further down and needs
    # this fact whether or not any battery iteration happens to run
    # first.
    spike_overrides_p2p_at_t0 = bool(batteries) and (
        batteries[0].available and batteries[0].spike_override_discharge_kw is not None
    )

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
    # nimbus issue #467: one 5-variable family PER battery, name-keyed --
    # same dict[str, list[str]] pattern as adequacy_vars below. Only
    # batteries[0] gets the P2P fixed-export charge gate (see this
    # function's own docstring, "batteries" paragraph, for the open
    # household decision this encodes); every other battery charges
    # under its own plain max_charge_kw ceiling.
    charge_vars: dict[str, list[str]] = {}
    discharge_vars: dict[str, list[str]] = {}
    soc_vars: dict[str, list[str]] = {}
    underfill_vars: dict[str, list[str]] = {}
    overfill_vars: dict[str, list[str]] = {}
    for b_idx, b in enumerate(batteries):
        # nimbus issue #563 item 2: b.available=False makes both
        # directions mathematically impossible for this WHOLE solve
        # (ub=0.0), the same "hard-impossible" technique the P2P fixed-
        # window charge gate below already uses -- see BatteryConfig's
        # own `available` docstring for why this is whole-horizon, not a
        # per-period mask.
        # nimbus issue #567: a real-time "sell into a price spike, right
        # now" household decision -- only ever period 0, only ever
        # batteries[0] (see BatteryConfig.spike_override_discharge_kw's
        # own docstring for the full mechanism/scoping). Pins BOTH
        # variables at t==0 to make the override mathematically
        # impossible to deviate from, the same hard-pin technique the
        # P2P fixed-export charge gate immediately below already uses --
        # not merely costed against.
        spike_override_kw = b.spike_override_discharge_kw
        spike_active_now = b_idx == 0 and b.available and spike_override_kw is not None
        # mypy issue #384-adjacent: spike_active_now already guarantees
        # spike_override_kw is not None wherever it's actually read below,
        # but mypy can't follow that through a separately-stored bool --
        # a real float pinned once here (only meaningful when spike_
        # active_now is True) sidesteps the false-positive Optional
        # narrowing error without an unchecked `# type: ignore`.
        spike_override_value = (
            spike_override_kw if spike_override_kw is not None else 0.0
        )
        charge_vars[b.name] = [
            p.add_variable(
                f"battery_charge_{b.name}_{t}",
                lb=0.0,
                ub=0.0
                if not b.available or (spike_active_now and t == 0)
                else (
                    p2p_export.charging_ub_during_fixed_window(t, grid, b.max_charge_kw)
                    if b_idx == 0
                    else b.max_charge_kw
                ),
            )
            for t in range(n)
        ]
        discharge_vars[b.name] = [
            p.add_variable(
                f"battery_discharge_{b.name}_{t}",
                lb=(spike_override_value if spike_active_now and t == 0 else 0.0),
                ub=0.0
                if not b.available
                else (
                    spike_override_value
                    if spike_active_now and t == 0
                    else b.max_discharge_kw
                ),
            )
            for t in range(n)
        ]
        # nimbus issue #328 (Mark Purcell): soc[t]'s only HARD bound is
        # now the true physical range [0, capacity_kwh] -- min_soc_kwh/
        # max_soc_kwh are enforced as a SOFT preference via underfill/
        # overfill below, not a bound on this variable itself. See
        # build_plan()'s own docstring ("soft_soc_penalty_per_kwh") for
        # the full design and why this replaces the pre-#328 hard bound.
        soc_vars[b.name] = [
            p.add_variable(f"battery_soc_{b.name}_{t}", lb=0.0, ub=b.capacity_kwh)
            for t in range(n)
        ]
        # underfill[t] = max(0, min_soc_kwh - soc[t]), overfill[t] =
        # max(0, soc[t] - max_soc_kwh) -- both genuinely pinned to their
        # exact max(0, ...) value (not just upper-bounded) because
        # they're COSTED below: minimizing total cost always drives a
        # costed, otherwise-unconstrained-from-above slack variable down
        # to the smallest value its own constraint permits, which is
        # exactly the true violation amount. This same "pinned by cost +
        # one-sided inequality" property is what makes it safe to reuse
        # underfill[idx] inside the terminal-value segment-fill
        # construction and the discharge wash-trade guard further below,
        # instead of just being a standalone floor penalty -- see each
        # of those sites' own comments for why a naive re-relaxation
        # there would otherwise reopen a real gaming vector (the LP
        # could otherwise "unlock" extra terminal-value credit, or extra
        # discharge headroom, by pretending SoC is lower than it really
        # is).
        underfill_vars[b.name] = [
            p.add_variable(
                f"battery_soc_underfill_{b.name}_{t}", lb=0.0, ub=b.min_soc_kwh
            )
            for t in range(n)
        ]
        overfill_vars[b.name] = [
            p.add_variable(
                f"battery_soc_overfill_{b.name}_{t}",
                lb=0.0,
                ub=b.capacity_kwh - b.max_soc_kwh,
            )
            for t in range(n)
        ]
        for t in range(n):
            # soc[t] + underfill[t] >= min_soc_kwh
            p.add_ub_constraint(
                {soc_vars[b.name][t]: -1.0, underfill_vars[b.name][t]: -1.0},
                -b.min_soc_kwh,
            )
            # soc[t] - overfill[t] <= max_soc_kwh
            p.add_ub_constraint(
                {soc_vars[b.name][t]: 1.0, overfill_vars[b.name][t]: -1.0},
                b.max_soc_kwh,
            )
            # nimbus issue #338: the penalty is a bare $/kWh on the
            # STATE violation, deliberately NOT scaled by hours[t] -- see
            # this constant's own original comment (git history) for the
            # full "production 5-minute grid" dominance reasoning, which
            # applies identically per-battery here.
            p.set_cost(underfill_vars[b.name][t], soft_soc_penalty_per_kwh)
            p.set_cost(overfill_vars[b.name][t], soft_soc_penalty_per_kwh)
    grid_import = [
        p.add_variable(f"grid_import_{t}", lb=0.0, ub=float(import_limit_arr[t]))
        for t in range(n)
    ]
    # nimbus issue #390 (Mark Purcell): grid_import[t]'s hard ub above has no
    # slack -- a load the forecaster extrapolated above `import_limit_kw +
    # max_discharge_kw + solar_kw` for longer than the battery's own energy
    # can cover makes the WHOLE horizon infeasible, discarding every other,
    # perfectly feasible period along with it. Confirmed live: a 40-minute
    # EV-charging transient extrapolated flat overnight by the recursive
    # load forecaster (a separate, real forecaster bug, tracked and fixed
    # independently) needed ~90 kWh of battery support the pack didn't have
    # -- 46 minutes with no usable plan at all, including every solve cycle
    # for the next few genuinely-feasible hours.
    #
    # `grid_import_excess[t]` is a real, unbounded release valve: energy the
    # LP can draw above the configured cap, at a real cost (its own price
    # below, set once `effective_import_price` exists) plus a heavy penalty
    # -- so a genuine, unavoidable spike costs a lot and shows up as a real
    # number (`Plan.import_cap_breach_kwh`) rather than discarding the whole
    # plan. The LP will only ever reach for this when `grid_import[t]` is
    # already pinned at its own ub AND the battery/solar combination can't
    # cover the rest -- during any normal period it stays at 0 for free,
    # since using it always costs strictly more than staying within the
    # configured cap.
    grid_import_excess = [
        p.add_variable(f"grid_import_excess_{t}", lb=0.0) for t in range(n)
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
    #
    # nimbus issue #694: period 0 is the one exception -- when the
    # household's price-spike override is genuinely active this solve
    # (spike_overrides_p2p_at_t0, computed once above), a real P2P
    # commitment's own pin turns into a FLOOR (lb=committed rate,
    # ub=export_limit_kw) rather than an exact value, for period 0 only.
    # P2P still "remains" -- the committed rate is still a guaranteed
    # minimum, per the household's own explicit instruction -- but export
    # can now rise above it to carry whatever battery_discharge_home_0
    # (hard-pinned to the configured spike rate just above) produces
    # beyond what the P2P commitment alone was already moving.
    grid_export = []
    for t in range(n):
        export_lb, export_ub = p2p_export.grid_export_bounds(
            t,
            grid,
            float(export_limit_arr[t]),
            override_p2p=(t == 0 and spike_overrides_p2p_at_t0),
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
                p, f"export_bonus_{t}", float(export_limit_arr[t])
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
    # nimbus issue #492 (Signals 3/7 of #489): each load's own real
    # per-period upper bound, kept alongside shed_vars/adequacy_vars
    # (below) so LoadSignals' own intent-band classification can read
    # the SAME ub the LP itself was built against, not re-derive it.
    load_ub: dict[str, list[float]] = {}
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
        load_ub[sl.name] = max_shed

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
    # nimbus issue #477: `allowed`, when given, replaces the plain
    # earliest_period<=t<=deadline_period window for this bound only --
    # see AdequacyLoadConfig's own docstring on why the deadline
    # constraint below still sums through deadline_period regardless.
    # nimbus issue #612: `windows`, when given, replaces BOTH the single
    # earliest/deadline pair above AND `allowed` for this bound -- a
    # period is allowed to draw power if it falls inside ANY of the
    # load's own windows (they're already validated non-overlapping and
    # in order in AdequacyLoadConfig.__post_init__), 0 otherwise.
    adequacy_vars: dict[str, list[str]] = {}
    for al in adequacy_loads:
        if al.windows is not None:
            ub_list = [
                al.max_power_kw
                if any(w.earliest_period <= t <= w.deadline_period for w in al.windows)
                else 0.0
                for t in range(n)
            ]
            adequacy_vars[al.name] = [
                p.add_variable(f"adequacy_{al.name}_{t}", lb=0.0, ub=ub_list[t])
                for t in range(n)
            ]
            load_ub[al.name] = ub_list
            continue
        ub_list = [
            al.max_power_kw
            if (
                bool(al.allowed[t])
                if al.allowed is not None
                else al.earliest_period <= t <= al.deadline_period
            )
            else 0.0
            for t in range(n)
        ]
        adequacy_vars[al.name] = [
            p.add_variable(f"adequacy_{al.name}_{t}", lb=0.0, ub=ub_list[t])
            for t in range(n)
        ]
        load_ub[al.name] = ub_list
    # nimbus issue #613 item 2: a small earliness preference on every
    # adequacy load's own power variable -- see DEFAULT_ADEQUACY_
    # EARLINESS_BUDGET_KW's own docstring for the full derivation. Cost
    # per period t is `budget_per_hour * elapsed_hours[t]` ($/kWh scaled
    # to this period's own duration, same unit convention as every other
    # per-period cost in this function, e.g. adequacy_credit_*'s own
    # `value_arr[t] * hours[t]`), where `elapsed_hours[t]` is the real
    # time from right now (period 0's own start) to period t's own
    # start -- period 0 itself always costs exactly 0 (never penalized
    # for running immediately), and the LATEST period in the whole
    # horizon costs exactly `adequacy_earliness_budget_kw` more, by
    # construction, regardless of any individual load's own earliest/
    # deadline window. Applied unconditionally to every adequacy load,
    # windowed or not -- deliberately no opt-out field (the issue's own
    # explicit ask: "No field for the household to set").
    #
    # A windowed load's own later windows (nimbus issue #612) still work
    # correctly here with this same GLOBAL, period-0-anchored elapsed_
    # hours array: each window's own deadline constraint and shortfall
    # slack are already independent per-window (see the deadline-
    # constraint block below), so within any one window this array is
    # still monotonically increasing with t, which is the only property
    # this cost term needs to correctly bias that window's own delivery
    # toward its own earliest feasible periods -- the fact that a later
    # window's periods carry a bigger absolute elapsed_hours value than
    # an earlier window's never matters, since the two windows never
    # compete for the same shortfall slack or deadline constraint.
    if adequacy_loads and adequacy_earliness_budget_kw > 0.0:
        horizon_hours = float(np.sum(hours))
        if horizon_hours > 0.0:
            elapsed_hours = np.concatenate(
                ([0.0], np.cumsum(hours, dtype=np.float64)[:-1])
            )
            earliness_rate = adequacy_earliness_budget_kw / horizon_hours
            for al in adequacy_loads:
                for t in range(n):
                    cost = earliness_rate * float(elapsed_hours[t]) * float(hours[t])
                    if cost != 0.0:
                        if _use_secondary_costs:
                            p.set_secondary_cost(adequacy_vars[al.name][t], cost)
                        else:
                            p.set_cost(adequacy_vars[al.name][t], cost)
    # nimbus issue #616 (semi-continuous + single-block, prior art:
    # EMHASS's treat_deferrable_load_as_semi_cont + set_deferrable_load_
    # single_constant): every real Controllable Load in this project is
    # commanded through a single on/off (or mode) service call --
    # switch.turn_on/turn_off, water_heater.set_operation_mode -- see
    # controllable_load_subentry.py's own device_entity selector comment
    # ("switch/water_heater today, climate expected later"). There is no
    # dimmer/number-domain dispatch path at all today, so letting an
    # AdequacyLoadConfig deliver a fractional power level (Mark's own
    # real report: 0.072, 0.384, 0.65, 0.65... kW across consecutive
    # periods for a device that can only ever be fully on or fully off)
    # was never a plan the household could actually execute -- not a
    # modelling convenience being given up, a bug being fixed. Two
    # constraints together: each period's power is EXACTLY 0 or EXACTLY
    # max_power_kw (never in between), and across its own [earliest_
    # period, deadline_period] window (or, for a #612 windowed load,
    # independently within EACH window -- same per-window independence
    # reasoning as the deadline constraint below) the load turns on at
    # MOST ONCE, so a household sees one clean block instead of several
    # short bursts burning #484's own daily activation cap on plan
    # artefacts. set_deferrable_startup_penalty (the third EMHASS
    # mechanism #616 names) is deliberately NOT implemented as a
    # separate priced field: with at-most-one-start already a HARD
    # constraint, there is nothing left for a penalty to discourage --
    # it would be a constant added to the objective with zero effect on
    # the solution. On by default, no household-facing field, same
    # "no consumer parameter" posture as #613's own earliness term.
    if adequacy_loads and adequacy_semi_continuous:
        for al in adequacy_loads:
            if al.windows is not None:
                block_ranges = [
                    list(range(w.earliest_period, w.deadline_period + 1))
                    for w in al.windows
                ]
            elif al.allowed is not None:
                block_ranges = [[t for t in range(n) if al.allowed[t]]]
            else:
                block_ranges = [list(range(al.earliest_period, al.deadline_period + 1))]
            for idxs in block_ranges:
                if not idxs:
                    continue
                on_vars = [
                    p.add_variable(f"adequacy_on_{al.name}_{t}", binary=True)
                    for t in idxs
                ]
                start_vars = [
                    p.add_variable(f"adequacy_start_{al.name}_{t}", binary=True)
                    for t in idxs
                ]
                for i, t in enumerate(idxs):
                    p.add_eq_constraint(
                        {
                            adequacy_vars[al.name][t]: 1.0,
                            on_vars[i]: -al.max_power_kw,
                        },
                        0.0,
                        name=f"adequacy_semicont_{al.name}_{t}",
                    )
                    # start[t] >= on[t] - on[t-1] (on[t-1] treated as 0
                    # when t-1 isn't itself part of this same contiguous
                    # `idxs` run -- e.g. a genuinely non-contiguous
                    # `allowed` mask -- so re-entering after a real gap
                    # correctly registers as a fresh start).
                    terms: dict[str, float] = {on_vars[i]: 1.0, start_vars[i]: -1.0}
                    if i > 0 and idxs[i - 1] == t - 1:
                        terms[on_vars[i - 1]] = -1.0
                    p.add_ub_constraint(
                        terms, 0.0, name=f"adequacy_start_link_{al.name}_{t}"
                    )
                p.add_ub_constraint(
                    {sv: 1.0 for sv in start_vars},
                    1.0,
                    name=f"adequacy_single_block_{al.name}_{idxs[0]}",
                )
    # nimbus issue #477: a soft shortfall slack per adequacy load,
    # replacing the old hard deadline constraint -- see
    # AdequacyLoadConfig's own docstring for the full "why" (matches
    # #390's grid_import_excess pattern). Bounded [0, target_kwh]: the
    # LP can never claim a shortfall larger than the target itself.
    #
    # nimbus issue #612: a windowed load gets its OWN shortfall slack
    # per window instead (adequacy_window_shortfall_vars below) -- one
    # window's own slack must never let a LATER window's target be met
    # for free, which sharing a single slack across windows would allow.
    # No single-load slack is created for a windowed load at all (it
    # would be a real, unused, always-slack variable -- harmless to
    # HiGHS but pointless to add).
    adequacy_shortfall_vars: dict[str, str] = {
        al.name: p.add_variable(
            f"adequacy_shortfall_{al.name}", lb=0.0, ub=al.target_kwh
        )
        for al in adequacy_loads
        if al.windows is None
    }
    for al in adequacy_loads:
        if al.windows is None:
            p.set_cost(adequacy_shortfall_vars[al.name], al.shortfall_price)
    adequacy_window_shortfall_vars: dict[str, list[str]] = {}
    for al in adequacy_loads:
        if al.windows is None:
            continue
        adequacy_window_shortfall_vars[al.name] = [
            p.add_variable(
                f"adequacy_shortfall_{al.name}_w{i}", lb=0.0, ub=w.target_kwh
            )
            for i, w in enumerate(al.windows)
        ]
        for var in adequacy_window_shortfall_vars[al.name]:
            p.set_cost(var, al.shortfall_price)
        if al.value_per_kwh is not None:
            # nimbus issue #606 (Mark Purcell, real finding: a 0.65kW
            # heat pump with value_per_kwh configured would have run at
            # MAX POWER in every single period the switchboard's shadow
            # price sat below the credited value -- crediting raw
            # power[t] directly, with nothing capping the total credited
            # energy at the load's own target_kwh, "keeps the load on at
            # max power in every cheap period and the published plan
            # overstates energy and cost" (issue's own words). The whole
            # POINT of value_per_kwh is "run early when it's cheap, not
            # forever" -- #482's own docstring already says as much
            # ("becomes a pure price-gated load... runs exactly where
            # the switchboard's own shadow price is <= this value"), but
            # nothing in the LP actually enforced the "only up to the
            # real target" half of that promise until now.
            #
            # Fix (Mark's own proposal 1, credited to EMHASS's
            # deferrable_load_max_cost / HAEO's consumption_cost
            # semantics per #603): a new served_credit[t] variable,
            # bounded above by power[t] each period (can never credit
            # more than what's actually delivered that period) AND by
            # the load's own real target_kwh in aggregate across the
            # whole window (can never credit more, cumulatively, than
            # the load genuinely needs) -- the credit is paid on
            # served_credit, never on raw power directly. A load that
            # genuinely wants to run past its target for real ongoing
            # value should set a per-period value on a LoadConfig/
            # SheddableLoadConfig instead; an AdequacyLoadConfig's whole
            # identity is a bounded, one-time target, and this credit
            # must respect that same bound, not quietly exceed it.
            value_arr = np.broadcast_to(
                np.asarray(al.value_per_kwh, dtype=np.float64), (n,)
            )
            credit_vars = [
                p.add_variable(
                    f"adequacy_credit_{al.name}_{t}", lb=0.0, ub=al.max_power_kw
                )
                for t in range(n)
            ]
            for t in range(n):
                p.add_ub_constraint(
                    {credit_vars[t]: 1.0, adequacy_vars[al.name][t]: -1.0},
                    0.0,
                    name=f"adequacy_credit_le_power_{al.name}_{t}",
                )
                p.set_cost(credit_vars[t], -float(value_arr[t]) * hours[t])
            # nimbus issue #612: a windowed load's real cumulative target
            # across the whole horizon is the SUM of its own windows, not
            # the single legacy target_kwh field (which windows replaces
            # for bounding/deadline purposes elsewhere in this function)
            # -- capping credit at the single-window figure here would
            # under-cap a multi-day load's real earned credit.
            credit_cap = (
                sum(w.target_kwh for w in al.windows)
                if al.windows is not None
                else al.target_kwh
            )
            p.add_ub_constraint(
                {credit_vars[t]: hours[t] for t in range(n)},
                credit_cap,
                name=f"adequacy_credit_cap_{al.name}",
            )

    # ---- Shared-circuit caps (SharedCircuitConfig, see its own
    # docstring) -- a real, physical headroom limit on the COMBINED power
    # of two or more adequacy loads at every single period, independent
    # of each member's own already-enforced individual max_power_kw. A
    # member_names entry not present in adequacy_vars is a real caller
    # mistake (a shared circuit referencing a load that was never passed
    # in adequacy_loads) -- raised here, loud and immediate, rather than
    # silently skipped or treated as a zero-power member.
    for sc in shared_circuits:
        missing = [name for name in sc.member_names if name not in adequacy_vars]
        if missing:
            msg = (
                f"Shared circuit '{sc.name}' references adequacy load(s) "
                f"{missing!r} that were not found in adequacy_loads -- "
                "every member_names entry must match a configured "
                "AdequacyLoadConfig's own name"
            )
            raise ValueError(msg)
        for t in range(n):
            terms = {adequacy_vars[name][t]: 1.0 for name in sc.member_names}
            p.add_ub_constraint(
                terms,
                sc.max_combined_power_kw,
                name=f"shared_circuit_{sc.name}_t{t}",
            )

    # ---- Cost terms ----
    # Each battery's own charge_cost/discharge_cost may be a plain scalar
    # (applied identically to every period) or a real per-period array
    # (2026-08-16, see BatteryConfig's own docstring) -- np.broadcast_to
    # normalizes both cases to a real length-n array up front, so the
    # loop below never needs to know which form the caller passed. A
    # caller-supplied array whose own length doesn't match n raises here
    # (a clear numpy broadcast error), not silently later.
    charge_cost_arrs: dict[str, NDArray[np.float64]] = {
        b.name: np.broadcast_to(np.asarray(b.charge_cost, dtype=np.float64), (n,))
        for b in batteries
    }
    discharge_cost_arrs: dict[str, NDArray[np.float64]] = {
        b.name: np.broadcast_to(np.asarray(b.discharge_cost, dtype=np.float64), (n,))
        for b in batteries
    }
    # Real economic cycle-wear cost (Track B2, elements.py's own
    # degradation_cost_per_kwh -- see that field's own docstring for the
    # full "why a separate additive term, not folded into charge_cost/
    # discharge_cost" reasoning). set_cost() is additive (see lp.py's
    # own docstring), so this simply layers on top of whatever TOU-
    # driven charge_cost/discharge_cost already priced -- 0.0 (the
    # default) is a genuine no-op, adds nothing to either cost.
    # nimbus issue #390: grid_import_excess[t]'s own penalty rate -- 10x the
    # single highest import price anywhere in this horizon, with a $5/kWh
    # floor for the degenerate case of an all-zero/near-zero price array (a
    # synthetic test grid, or a genuine data gap) where 10x a near-zero
    # price would be too cheap to actually deter reaching for this before
    # exhausting every real, cheaper option first.
    import_excess_penalty_rate = max(10.0 * float(np.max(effective_import_price)), 5.0)
    for t in range(n):
        p.set_cost(grid_import[t], effective_import_price[t] * hours[t])
        # Real energy at the real import price, PLUS the deterrent penalty
        # -- this is still genuinely-consumed grid energy, not a free
        # accounting fiction, so it keeps paying the real per-kWh rate on
        # top of what makes the LP avoid it whenever any cheaper option
        # (battery, a smaller shed, waiting) exists instead.
        p.set_cost(
            grid_import_excess[t],
            (effective_import_price[t] + import_excess_penalty_rate) * hours[t],
        )
        p.set_cost(grid_export[t], -effective_export_price[t] * hours[t])
        for b in batteries:
            p.set_cost(
                charge_vars[b.name][t],
                (charge_cost_arrs[b.name][t] + b.degradation_cost_per_kwh) * hours[t],
            )
            p.set_cost(
                discharge_vars[b.name][t],
                (discharge_cost_arrs[b.name][t] + b.degradation_cost_per_kwh)
                * hours[t],
            )
        for sl in sheddable_loads:
            p.set_cost(shed_vars[sl.name][t], sl.shed_cost * hours[t])
    # nimbus issue #692: a small earliness preference on every battery's
    # own charge variable -- same technique, same tiny fixed total-
    # budget-across-the-horizon derivation as adequacy_earliness_
    # budget_kw's own block below (see DEFAULT_BATTERY_CHARGE_EARLINESS_
    # BUDGET_KW's own docstring for the real, live evidence this fixes).
    # set_cost() is additive, so this simply layers a tiny extra cost on
    # top of whatever charge_cost/degradation_cost_per_kwh already
    # priced above -- period 0 is never penalized, the LATEST period in
    # the whole horizon costs exactly battery_charge_earliness_budget_kw
    # more, by construction, regardless of horizon length.
    if batteries and battery_charge_earliness_budget_kw > 0.0:
        horizon_hours = float(np.sum(hours))
        if horizon_hours > 0.0:
            elapsed_hours = np.concatenate(
                ([0.0], np.cumsum(hours, dtype=np.float64)[:-1])
            )
            battery_earliness_rate = battery_charge_earliness_budget_kw / horizon_hours
            for b in batteries:
                for t in range(n):
                    cost = (
                        battery_earliness_rate
                        * float(elapsed_hours[t])
                        * float(hours[t])
                    )
                    if cost != 0.0:
                        if _use_secondary_costs:
                            p.set_secondary_cost(charge_vars[b.name][t], cost)
                        else:
                            p.set_cost(charge_vars[b.name][t], cost)
    # Two-tier export bonus (see elements.py's own GridConfig docstring):
    # export_bonus[t] earns an EXTRA revenue credit on top of whatever
    # grid_export[t] already earns at the base rate above -- set_cost()
    # ADDS to an existing coefficient (see its own docstring, same
    # pattern already used for salvage_value/headroom_value below), but
    # export_bonus[t] is its own separate variable here, not sharing
    # grid_export[t]'s coefficient, so this is a plain new cost, not an
    # accumulation.
    if has_export_bonus:
        # mypy issue #384: export_bonus is list[str] | None, set together
        # with has_export_bonus at declaration -- mypy can't connect the
        # two, so this asserts what's already structurally guaranteed.
        assert export_bonus is not None
        for t in range(n):
            p2p_export.set_export_bonus_cost(p, export_bonus[t], t, grid, hours)
    # nimbus issue #467: terminal value (piecewise curve OR flat salvage/
    # headroom) is computed PER BATTERY -- each has its own independent
    # terminal_value_breakpoints/salvage_value/headroom_value on its own
    # BatteryConfig, exactly the fields this loop already reads.
    for b in batteries:
        if b.terminal_value_breakpoints is not None:
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
                b.terminal_value_period_indices
                if b.terminal_value_period_indices is not None
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
                scale = (
                    1.0 if idx == n - 1 or n_intermediate == 0 else 1.0 / n_intermediate
                )
                seg_vars = [
                    p.add_variable(f"terminal_seg_{b.name}_{idx}_{i}", lb=0.0, ub=width)
                    for i, (width, _rate) in enumerate(b.terminal_value_breakpoints)
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
                        soc_vars[b.name][idx]: -1.0,
                        underfill_vars[b.name][idx]: -1.0,
                    },
                    -b.min_soc_kwh,
                    name=f"terminal_value_segments_fill_{b.name}_{idx}",
                )
                for seg, (_width, rate) in zip(
                    seg_vars, b.terminal_value_breakpoints, strict=True
                ):
                    p.set_cost(seg, -rate * scale)
        else:
            # Salvage value: a one-time credit on the FINAL period's soc -- without
            # this, a finite-horizon LP has no reason to ever hold charge past the
            # last period it can see, and will always drain to its own min_soc on
            # the final tick (see the architecture sketch's own §6 "Salvage value,
            # in plain terms" explainer).
            p.set_cost(soc_vars[b.name][n - 1], -b.salvage_value)
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
            p.set_cost(soc_vars[b.name][n - 1], b.headroom_value)

    # ---- Stability mechanisms 1 & 2 (see module docstring) ----
    prev_grid_import = (
        previous_plan.grid_import_kw if previous_plan is not None else None
    )
    prev_grid_export = (
        previous_plan.grid_export_kw if previous_plan is not None else None
    )
    for var_names, family, prev_values in (
        (grid_import, "grid_import", prev_grid_import),
        (grid_export, "grid_export", prev_grid_export),
    ):
        _add_proximal_penalty(
            p,
            var_names,
            family,
            alignment,
            prev_values,
            hours,
            proximal_weight,
            use_secondary=_use_secondary_costs,
        )
        if max_rate_kw is not None:
            _add_rate_limit(
                p, var_names, family, n, alignment, prev_values, max_rate_kw
            )
        _add_intraplan_smoothness_penalty(
            p,
            var_names,
            family,
            n,
            hours,
            smoothness_weight,
            use_secondary=_use_secondary_costs,
        )

    # nimbus issue #467: per-participant cross-solve stability, matched
    # by NAME against the previous solve's own Plan.batteries -- the
    # "real net-new plumbing" Mark's spec calls out. A battery whose
    # name doesn't appear in previous_plan.batteries (a genuinely new
    # participant, or a previous_plan reconstructed from a persisted
    # state file that predates this field -- see Plan.batteries's own
    # docstring) simply gets no continuity this solve, same graceful
    # fallback _align_previous_periods() already uses for "nothing to
    # align against". Unlike grid_import/grid_export above (one real
    # grid connection, family names stay flat), each battery's own
    # family is keyed "charge_{name}"/"discharge_{name}" so battery A's
    # own continuity can never read against battery B's previous values.
    prev_batteries_by_name: dict[str, BatteryPlan] = (
        {bp.name: bp for bp in previous_plan.batteries}
        if previous_plan is not None
        else {}
    )
    for b in batteries:
        prev_bp = prev_batteries_by_name.get(b.name)
        prev_charge_b = prev_bp.charge_kw if prev_bp is not None else None
        prev_discharge_b = prev_bp.discharge_kw if prev_bp is not None else None
        for var_names, family, prev_values in (
            (charge_vars[b.name], f"charge_{b.name}", prev_charge_b),
            (discharge_vars[b.name], f"discharge_{b.name}", prev_discharge_b),
        ):
            _add_proximal_penalty(
                p,
                var_names,
                family,
                alignment,
                prev_values,
                hours,
                proximal_weight,
                use_secondary=_use_secondary_costs,
            )
            if max_rate_kw is not None:
                _add_rate_limit(
                    p, var_names, family, n, alignment, prev_values, max_rate_kw
                )
            _add_intraplan_smoothness_penalty(
                p,
                var_names,
                family,
                n,
                hours,
                smoothness_weight,
                use_secondary=_use_secondary_costs,
            )

    # nimbus issue #478 (carry-over continuity, real risk flagged during
    # #616's own verification): #616's semi-continuous/single-block
    # constraint makes an adequacy load's own plan choice a hard,
    # discrete jump between periods rather than a smooth continuous
    # reallocation -- without an anchor to the previous solve's own
    # committed placement, a late price-forecast swing could relocate a
    # half-delivered block instead of continuing it (verified directly:
    # network.py had no continuity mechanism for adequacy_vars at all,
    # only grid_import/export and battery charge/discharge above). Same
    # proximal-penalty mechanism, matched by NAME against the previous
    # solve's own Plan.adequacy_loads -- same graceful "no prior plan /
    # new load -> no continuity this solve" fallback every other family
    # above already gets. This is a SOFT nudge (same materiality-bounded
    # weight as everything else _add_proximal_penalty backs), not a hard
    # forcing of the previous on/off state -- #478's own fuller carry-
    # over spec (on_periods_elapsed/off_periods_elapsed forcing a
    # currently-running block to continue even against a real, if small,
    # economic preference elsewhere) remains open, this only fixes the
    # "arbitrary relocation between economically-tied placements" half.
    prev_adequacy_by_name: dict[str, AdequacyLoadPlan] = (
        {alp.name: alp for alp in previous_plan.adequacy_loads}
        if previous_plan is not None
        else {}
    )
    for al in adequacy_loads:
        prev_alp = prev_adequacy_by_name.get(al.name)
        prev_power = prev_alp.power_kw if prev_alp is not None else None
        _add_proximal_penalty(
            p,
            adequacy_vars[al.name],
            f"adequacy_{al.name}",
            alignment,
            prev_power,
            hours,
            proximal_weight,
            use_secondary=_use_secondary_costs,
        )

    # ---- SoC dynamics -- each battery's own independent recursion ----
    for b in batteries:
        for t in range(n):
            prev = b.initial_soc_kwh if t == 0 else None
            terms = {
                soc_vars[b.name][t]: 1.0,
                charge_vars[b.name][t]: -b.charge_efficiency * hours[t],
                discharge_vars[b.name][t]: hours[t] / b.discharge_efficiency,
            }
            if prev is None:
                terms[soc_vars[b.name][t - 1]] = -1.0
                p.add_eq_constraint(terms, 0.0)
            else:
                p.add_eq_constraint(terms, prev)

    # ---- Departure-deadline hard floor (nimbus issue #563 item 2) ----
    # soc[idx] >= must_have_soc_kwh, expressed as -soc[idx] <= -target
    # (LPProblem only has <=, same negation technique the adequacy
    # deadline constraint further below uses). HARD, not soft, on
    # purpose -- a real EV genuinely needs a real SoC by a real
    # departure time, not a priced preference the LP can trade away.
    # No-op when either field is None (BatteryConfig.__post_init__
    # already guarantees both-or-neither) OR when this solve's own
    # horizon doesn't reach that period index yet -- a household's
    # departure hour simply being beyond a short manual solve window is
    # a normal, expected case, not a misconfiguration; silently skipping
    # here (not raising) is deliberate, matching this file's own
    # terminal_value_period_indices validation, which only rejects an
    # index beyond the horizon at build_plan()'s own top-level guard,
    # never here mid-construction.
    for b in batteries:
        # Both-None-or-both-set is a real BatteryConfig.__post_init__
        # invariant, but this explicit check (rather than trusting that
        # invariant silently) is what lets mypy narrow must_have_soc_kwh
        # from `float | None` to `float` below -- the type checker has
        # no way to see across dataclass construction into this
        # unrelated function.
        if b.must_have_soc_by_period_index is None or b.must_have_soc_kwh is None:
            continue
        if b.must_have_soc_by_period_index >= n:
            continue
        p.add_ub_constraint(
            {soc_vars[b.name][b.must_have_soc_by_period_index]: -1.0},
            -b.must_have_soc_kwh,
            name=f"battery_departure_deadline_{b.name}",
        )

    # ---- Shared-charger group cap (nimbus issue #563 item 3) ----
    # Two or more participants sharing the SAME non-None
    # shared_charger_group name draw from one real physical charger --
    # sum(charge[t]+discharge[t]) across the group <= that group's own
    # ceiling, per period. Deliberately SEPARATE from the per-battery
    # wash-trade cap just above (#245/#467, kept strictly per-
    # participant -- two independent batteries legitimately charging/
    # discharging at the same time is real, not a wash trade); this is
    # an ADDITIONAL constraint on top, only for participants that
    # explicitly opt into sharing one real resource. An ungrouped
    # battery (shared_charger_group=None, the default) never appears in
    # any group here -- zero effect, byte-identical to every scenario
    # before this mechanism existed.
    _charger_groups: dict[str, list[BatteryConfig]] = {}
    for b in batteries:
        if b.shared_charger_group is not None:
            _charger_groups.setdefault(b.shared_charger_group, []).append(b)
    for group_name, members in _charger_groups.items():
        # Conservative reading when a household's config disagrees with
        # itself across two subentries describing the one real charger
        # (see BatteryConfig.shared_charger_max_kw's own docstring) --
        # the MINIMUM non-None value declared. A group where NO member
        # declared a ceiling has nothing to constrain against and is
        # silently skipped, not an error -- the shared_charger_group
        # name alone with no kW figure is an honest partial config, the
        # same "skip, don't crash" posture solver_writer.py's own
        # build_extra_batteries() already uses for a missing field.
        declared = [
            m.shared_charger_max_kw
            for m in members
            if m.shared_charger_max_kw is not None
        ]
        if not declared:
            continue
        ceiling = min(declared)
        for t in range(n):
            terms = {}
            for m in members:
                terms[charge_vars[m.name][t]] = (
                    terms.get(charge_vars[m.name][t], 0.0) + 1.0
                )
                terms[discharge_vars[m.name][t]] = (
                    terms.get(discharge_vars[m.name][t], 0.0) + 1.0
                )
            p.add_ub_constraint(
                terms, ceiling, name=f"shared_charger_{group_name}_t{t}"
            )

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
            grid_import[t]: 1.0,
            grid_import_excess[t]: 1.0,
            grid_export[t]: -1.0,
        }
        # nimbus issue #467: sum every battery's own discharge (+1.0
        # supply term)/charge (-1.0 demand term) into the SAME balance
        # row -- same mechanical pattern already used just below for
        # folding in sheddable/adequacy loads, generalized from the
        # single flat discharge[t]/charge[t] terms this row used to
        # carry directly.
        for b in batteries:
            terms[discharge_vars[b.name][t]] = (
                terms.get(discharge_vars[b.name][t], 0.0) + 1.0
            )
            terms[charge_vars[b.name][t]] = terms.get(charge_vars[b.name][t], 0.0) - 1.0
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
        # solar surplus or genuine battery discharge -- from ANY
        # battery, nimbus issue #467 sums across the whole `batteries`
        # list here, since a real household meter only sees the SYSTEM's
        # combined discharge, not which participant it came from -- never
        # a same-period grid_import[t]: grid_export[t] - solar_used[t] -
        # sum(discharge) <= 0.
        p.add_ub_constraint(
            {
                grid_export[t]: 1.0,
                solar_used[t]: -1.0,
                **{discharge_vars[b.name][t]: -1.0 for b in batteries},
            },
            0.0,
        )
        for b in batteries:
            # (2) Battery-routed pathway (PER BATTERY, nimbus issue #467
            # -- each battery can only draw on ITS OWN previously-
            # existing SoC, never another battery's): discharge[t] can
            # only draw on SoC that genuinely existed BEFORE this
            # period's own charging, never energy added within the same
            # period -- discharge[t]*hours[t]/discharge_efficiency <=
            # soc[t-1] - min_soc_kwh (b.initial_soc_kwh stands in for
            # soc[-1] at t=0, a known constant, so it moves straight to
            # the RHS rather than needing a variable term).
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
            # b.initial_soc_kwh is a known constant, so the equivalent
            # max(0, ...) is computed directly in Python rather than via an
            # LP variable, with the identical effect.
            draw_coeff = hours[t] / b.discharge_efficiency
            if t == 0:
                p.add_ub_constraint(
                    {discharge_vars[b.name][t]: draw_coeff},
                    max(0.0, b.initial_soc_kwh - b.min_soc_kwh),
                )
            else:
                p.add_ub_constraint(
                    {
                        discharge_vars[b.name][t]: draw_coeff,
                        soc_vars[b.name][t - 1]: -1.0,
                        underfill_vars[b.name][t - 1]: -1.0,
                    },
                    -b.min_soc_kwh,
                )
            # (3) Combined-direction cap (nimbus issue #245, kept PER
            # PARTICIPANT under #467 -- two independent battery systems
            # legitimately charging and discharging simultaneously in
            # the same period is real, not a wash trade; pooling this
            # cap across batteries would incorrectly forbid that). The
            # physical battery has one DC current direction at any
            # instant -- it cannot charge and discharge simultaneously,
            # so charge[t] and discharge[t] (independent LP variables
            # with no link between them otherwise) left an unconstrained
            # degeneracy budget wide open. A bad upstream price signal
            # (nimbus issue #236) let the LP inflate both freely in the
            # same period -- e.g. charge=17.98 + discharge=16.91 kW,
            # netting to the real -1.06 kW charge the LP had actually
            # decided on, with the rest pure wash-trade noise nothing
            # pinned down. This single linear constraint kills that
            # budget without a MILP reformulation: charge[t] +
            # discharge[t] <= max(max_charge_kw, max_discharge_kw). On
            # any normal row only one side is ever nonzero, so the cap
            # sits above both individual ub's already in force and
            # changes nothing; it only binds on a wash-trade row,
            # forcing the LP back to its real net. (A true `charge[t]*
            # discharge[t] == 0` complementarity needs a binary per
            # period -- MILP, tracked separately as issue #238 -- but
            # the objective already has no incentive for simultaneous
            # nonzero once #242 landed, so this linear cap is sufficient
            # in practice.)
            p.add_ub_constraint(
                {charge_vars[b.name][t]: 1.0, discharge_vars[b.name][t]: 1.0},
                max(b.max_charge_kw, b.max_discharge_kw),
            )
        # (4) Two-tier export bonus (see elements.py's own GridConfig
        # docstring): export_bonus[t] can never exceed that SAME period's
        # real total export[t] -- can't claim bonus volume for export
        # that never actually happened -- export_bonus[t] - grid_export[t]
        # <= 0.
        if has_export_bonus:
            assert export_bonus is not None  # mypy issue #384, see above
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
            max(float(import_limit_arr[t]), float(export_limit_arr[t])),
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
    # soc[t-1] is each battery's own initial_soc_kwh (a known constant) at t==0,
    # same convention as the wash-trade-prevention constraint (2) just
    # above -- moves straight to the RHS rather than needing a variable
    # term.
    for b in batteries:
        for var_list, curve in (
            (charge_vars[b.name], b.charge_power_curve),
            (discharge_vars[b.name], b.discharge_power_curve),
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
                        rhs = intercept + slope * b.initial_soc_kwh
                        p.add_ub_constraint({var_list[t]: 1.0}, rhs)
                    else:
                        p.add_ub_constraint(
                            {var_list[t]: 1.0, soc_vars[b.name][t - 1]: -slope},
                            intercept,
                        )

    # ---- Adequacy deadline constraints -- one inequality per adequacy
    # load, NOT per period: cumulative energy delivered through the
    # deadline, PLUS the shortfall slack (nimbus issue #477), must reach
    # target_kwh. LPProblem only has <=, so this is expressed as
    # -sum(power*hours) - shortfall <= -target_kwh. The sum always runs
    # 0..deadline_period (not earliest_period..deadline_period) -- when
    # `allowed` is given, periods outside it already have their own
    # variable bounded to 0 above, so including them here is harmless
    # and correct; using 0 as the start avoids this constraint silently
    # disagreeing with a real `allowed` mask that doesn't line up with
    # earliest_period. A genuinely expensive-to-fully-serve target no
    # longer makes the whole plan infeasible -- it costs shortfall_price
    # per kWh short instead, a real priced tradeoff visible in the
    # output rather than a solver-wide failure.
    for al in adequacy_loads:
        if al.windows is not None:
            # nimbus issue #612: one constraint PER WINDOW, each summed
            # ONLY over that window's own [earliest_period, deadline_period]
            # range -- not from 0 like the single-window case just below.
            # Windows are chronologically disjoint (validated in
            # AdequacyLoadConfig.__post_init__), so summing a later
            # window from 0 would double-count an earlier window's own
            # already-delivered energy toward a target it was never
            # meant to satisfy -- the whole point of #612 is that EACH
            # day owes its own fresh target, not one target amortized
            # across the full horizon.
            for i, w in enumerate(al.windows):
                window_range = range(w.earliest_period, w.deadline_period + 1)
                terms = {adequacy_vars[al.name][t]: -hours[t] for t in window_range}
                terms[adequacy_window_shortfall_vars[al.name][i]] = -1.0
                p.add_ub_constraint(
                    terms, -w.target_kwh, name=f"adequacy_deadline_{al.name}_w{i}"
                )
            continue
        window = range(al.deadline_period + 1)
        terms = {adequacy_vars[al.name][t]: -hours[t] for t in window}
        terms[adequacy_shortfall_vars[al.name]] = -1.0
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
        assert export_bonus is not None  # mypy issue #384, see above
        # Per-real-calendar-day cumulative cap + latest-preferred
        # tie-breaker -- extracted to p2p_export.py (nimbus issue #355),
        # see that module's own add_export_bonus_cumulative_caps()
        # docstring for the full "why per-day not global" and "why
        # latest not earliest" reasoning (both real, live household
        # findings, not design choices made in the abstract).
        p2p_export.add_export_bonus_cumulative_caps(
            p, {t: export_bonus[t] for t in range(n)}, periods, grid
        )

    # nimbus issue #491: ranging is opt-in via `compute_signals` (see
    # build_plan()'s own docstring for why this isn't unconditional --
    # a real measured timing regression on this project's own most
    # LP-structurally-complex real scenario).
    #
    # nimbus issue #696/#702: `_use_secondary_costs` (computed once, up
    # front, from config alone -- see its own comment above) is the
    # single source of truth for whether this solve actually engages the
    # caller's requested `solve_options` -- False only when the caller
    # passed `solve_options=None` in the first place. `LPProblem.solve()`
    # itself now handles the MIP case (see its own docstring, #702) via
    # pin-and-relax, so there is no MIP-specific fallback left here.
    result: LPResult = p.solve(
        ranging=compute_signals,
        keep_basis=compute_offer_curve,
        options=solve_options if _use_secondary_costs else None,
    )
    if result.status != "optimal":
        return _infeasible_plan(
            periods, result.status, result.iterations, raw_status=result.raw_status
        )

    def _get(names: list[str]) -> NDArray[np.float64]:
        return p.values_of(result, names)

    plan_sheddable = [
        SheddableLoadPlan(
            name=sl.name,
            # mypy issue #384: numpy's own stubs widen a float64 array
            # arithmetic result to floating[Any] -- a real stub-precision
            # gap, not a real bug (see this project's own CLAUDE.md GBRT-
            # vs-k-NN finding for the general pattern); explicit dtype
            # keeps the real, narrower float64 contract Plan/
            # SheddableLoadPlan both declare.
            served_kw=(sl.forecast_kw - _get(shed_vars[sl.name])).astype(np.float64),
            shed_kw=_get(shed_vars[sl.name]),
            subentry_id=sl.subentry_id,
        )
        for sl in sheddable_loads
    ]
    plan_adequacy = []
    for al in adequacy_loads:
        power_arr = _get(adequacy_vars[al.name])
        if al.windows is not None:
            # nimbus issue #612: no single "the deadline" any more --
            # delivered_by_deadline_kwh becomes the real total delivered
            # across the WHOLE horizon (every window combined). That
            # field genuinely has zero downstream consumers today
            # (checked directly, solver_writer.py never reads it), so
            # the aggregate-total shape is still safe for it.
            # shortfall_kwh sums every window's own independent slack.
            # Neither field is consumed downstream of network.py today
            # (checked directly) beyond this aggregate-total shape, so
            # this is a safe, honest generalization of the single-window
            # meaning rather than a behavior-preserving requirement.
            delivered_by_deadline_kwh = float(np.sum(power_arr * hours))
            shortfall_kwh = sum(
                p.value_of(result, var)
                for var in adequacy_window_shortfall_vars[al.name]
            )
        else:
            delivered_by_deadline_kwh = float(
                np.sum(
                    power_arr[0 : al.deadline_period + 1]
                    * hours[0 : al.deadline_period + 1]
                )
            )
            shortfall_kwh = p.value_of(result, adequacy_shortfall_vars[al.name])
        # nimbus issue #482: "what did the miner earn over what its
        # energy was worth" -- None (not 0.0) when this load has no
        # value_per_kwh configured at all, same "never fabricate a
        # number that isn't real" posture as AdequacyLoadPlan's own
        # docstring. lambda(t) read straight from the same power_
        # balance_t{t} dual #613's own per-period shadow_price exposes,
        # with the identical #662 hours-scaling correction (the raw dual
        # comes out in "$ per kW of RHS," not $/kWh, until divided by
        # this period's own duration).
        profit_horizon: float | None = None
        if al.value_per_kwh is not None:
            value_arr = np.broadcast_to(
                np.asarray(al.value_per_kwh, dtype=np.float64), (n,)
            )
            lambda_arr = np.array(
                [
                    result.duals.get(f"power_balance_t{t}", 0.0) / hours[t]
                    for t in range(n)
                ]
            )
            profit_horizon = float(np.sum((value_arr - lambda_arr) * power_arr * hours))
        plan_adequacy.append(
            AdequacyLoadPlan(
                name=al.name,
                power_kw=power_arr,
                delivered_by_deadline_kwh=delivered_by_deadline_kwh,
                shortfall_kwh=shortfall_kwh,
                subentry_id=al.subentry_id,
                profit_horizon=profit_horizon,
            )
        )

    # nimbus issue #467: per-battery arrays first, then the summed
    # aggregate from those SAME arrays -- guarantees battery_charge_kw/
    # battery_discharge_kw/battery_soc_kwh (every existing reader's own
    # field) is byte-identical to "sum of Plan.batteries", never a
    # separately-computed figure that could silently drift from it.
    plan_batteries = [
        BatteryPlan(
            name=b.name,
            charge_kw=_get(charge_vars[b.name]),
            discharge_kw=_get(discharge_vars[b.name]),
            soc_kwh=_get(soc_vars[b.name]),
        )
        for b in batteries
    ]
    battery_charge_kw_total = sum(
        (bp.charge_kw for bp in plan_batteries), np.zeros(n)
    ).astype(np.float64)
    battery_discharge_kw_total = sum(
        (bp.discharge_kw for bp in plan_batteries), np.zeros(n)
    ).astype(np.float64)
    battery_soc_kwh_total = sum(
        (bp.soc_kwh for bp in plan_batteries), np.zeros(n)
    ).astype(np.float64)

    # nimbus issue #491 (Signals 2/7 of #489): grid-operator + per-battery
    # headroom/forced-cost signals, straight off result's own ranging
    # (see LPResult.bound_headroom()'s own docstring) and reduced costs.
    # None (not 0/fabricated) whenever ranging itself wasn't valid for
    # this solve -- same honest-diagnostic posture as duals/reduced_costs
    # already use elsewhere on this class.
    def _headroom_up(var: str) -> float:
        h = result.bound_headroom(var)
        return h.up if h is not None else 0.0

    def _rhs_headroom(row: str) -> tuple[float, float]:
        h = result.rhs_headroom(row)
        return (h.up, h.down) if h is not None else (0.0, 0.0)

    if result.ranging_valid:
        grid_import_headroom_kw = np.array(
            [_headroom_up(f"grid_import_{t}") for t in range(n)]
        )
        grid_export_headroom_kw = np.array(
            [_headroom_up(f"grid_export_{t}") for t in range(n)]
        )
        forced_import_cost = np.array(
            [
                result.reduced_costs.get(f"grid_import_{t}", 0.0) / hours[t]
                for t in range(n)
            ]
        )
        forced_export_cost = np.array(
            [
                result.reduced_costs.get(f"grid_export_{t}", 0.0) / hours[t]
                for t in range(n)
            ]
        )
        # nimbus issue #492 (Signals 3/7 of #489): switchboard headroom
        # -- how much MORE/LESS total load this period could genuinely
        # absorb before its own real-time price λ(t) would change,
        # straight from the power_balance_t{t} row's own RHS ranging
        # (see GridSignals' own docstring for a real, hand-verified
        # example).
        _load_headroom = [_rhs_headroom(f"power_balance_t{t}") for t in range(n)]
        load_headroom_up_kwh = np.array([up for up, _dn in _load_headroom])
        load_headroom_down_kwh = np.array([dn for _up, dn in _load_headroom])
        grid_signals: GridSignals | None = GridSignals(
            grid_import_headroom_kw=grid_import_headroom_kw,
            grid_import_headroom_kwh=(grid_import_headroom_kw * hours).astype(
                np.float64
            ),
            grid_export_headroom_kw=grid_export_headroom_kw,
            grid_export_headroom_kwh=(grid_export_headroom_kw * hours).astype(
                np.float64
            ),
            forced_import_cost=forced_import_cost,
            forced_export_cost=forced_export_cost,
            # nimbus issue #493 (not yet built): "min'd with the
            # envelope" is real, honestly-scoped follow-up work -- there
            # is no envelope to min against yet, so these two are simply
            # the plan-consistent headroom on its own for now.
            flex_available_up_kw=grid_import_headroom_kw,
            flex_available_down_kw=grid_export_headroom_kw,
            load_headroom_up_kwh=load_headroom_up_kwh,
            load_headroom_down_kwh=load_headroom_down_kwh,
        )
    else:
        grid_signals = None

    plan_battery_signals = [
        BatterySignals(
            name=b.name,
            available_up_kw=(
                b.max_charge_kw
                - plan_batteries[i].charge_kw
                + plan_batteries[i].discharge_kw
            ).astype(np.float64),
            available_down_kw=(
                b.max_discharge_kw
                - plan_batteries[i].discharge_kw
                + plan_batteries[i].charge_kw
            ).astype(np.float64),
            available_up_ranging_kw=(
                np.array(
                    [_headroom_up(f"battery_charge_{b.name}_{t}") for t in range(n)]
                )
                if result.ranging_valid
                else None
            ),
            available_down_ranging_kw=(
                np.array(
                    [_headroom_up(f"battery_discharge_{b.name}_{t}") for t in range(n)]
                )
                if result.ranging_valid
                else None
            ),
        )
        for i, b in enumerate(batteries)
    ]

    # nimbus issue #492 (Signals 3/7 of #489): per-load intent bands --
    # see LoadSignals' own docstring for the full classification rule.
    # Guarded on ranging_valid the same way grid_signals is (the whole
    # LIST is empty rather than per-field None, since every field here
    # fundamentally needs ranging, unlike BatterySignals' own physical/
    # ranging split).
    _INTENT_TOL = 1e-6

    def _load_signal(
        name: str,
        var_names: list[str],
        var_ub: list[float],
        *,
        mirror_against: NDArray[np.float64] | None = None,
    ) -> LoadSignals:
        """`mirror_against`, when given (a sheddable load's own risk-
        adjusted forecast), reframes the registered `shed_{name}_{t}`
        variable's own ranging into the household-facing SERVED/draw
        quantity instead (`served = mirror_against - shed`) -- verified
        directly against both of #492's own worked examples, which
        neither match classifying the raw shed variable itself: a
        cheap-window case where shedding costs nothing extra reports
        `UNLIMIT, band_max ~= forecast` (the DEVICE may draw anywhere up
        to its own forecast, not "shedding may range up to max_shed"),
        and a peak-price case where shedding is cheaper than serving
        reports `SET 0` for the device's own draw (shed pinned at its
        max, so served is pinned at 0) with a POSITIVE reduced cost on
        the served side (serving one more kWh there would cost the
        plan money) -- the raw shed variable's own reduced cost is
        negative in that same scenario, the opposite sign. `None`
        (adequacy loads): the registered variable already IS the
        device's own draw, no transform needed.

        Classifies from the REDUCED COST first, not the ranging band's
        own numeric width -- a real, hand-verified finding while
        building this (see a minimal two-variable LP: x in [0,3] cost
        -1, y >= 0 cost 2, x+y==5 -- x pins at its own ub=3, genuinely
        NOT a tie, yet `bound_headroom("x")` still reports a full
        `down=3.0, up=2.0` range). `col_bound_up`/`col_bound_dn`
        ranging answers "how far could this variable's own BOUND move
        before the optimal BASIS's qualitative structure changes" --
        NOT "how far could the CURRENT VALUE move while the objective
        stays flat". Those two questions coincide only when the
        variable is genuinely tied (reduced cost == 0, truly basic);
        for a variable pinned at a bound by a real cost preference, the
        structural range can be wide even though there is zero real
        economic flex. So: reduced cost decides UNLIMIT vs SET (the
        textbook meaning of a zero vs nonzero reduced cost -- basic vs
        bound-pinned); the structural ranging range is only used to
        size the band when UNLIMIT (a real, meaningful "how far this
        could move" figure for a genuinely free variable). SET reports
        the variable's own actual committed value as both band edges --
        its honest "limit", not a wider structural range that would
        mislead a household reading `SET 0` into `band_max: 3`."""
        x = _get(var_names)
        band_min = np.zeros(n, dtype=np.float64)
        band_max = np.zeros(n, dtype=np.float64)
        rc_kwh = np.zeros(n, dtype=np.float64)
        degenerate = np.zeros(n, dtype=np.bool_)
        intent: list[str] = []
        for t in range(n):
            h = result.bound_headroom(var_names[t])
            raw_val = float(x[t])
            raw_ub = float(var_ub[t])
            if h is not None:
                raw_lo = max(0.0, raw_val - h.down)
                raw_hi = min(raw_ub, raw_val + h.up)
                is_degenerate = h.degenerate
            else:
                raw_lo = raw_val
                raw_hi = raw_val
                is_degenerate = True
            raw_rc = result.reduced_costs.get(var_names[t], 0.0) / hours[t]
            if mirror_against is not None:
                ceiling = float(mirror_against[t])
                val = ceiling - raw_val
                lo, hi = ceiling - raw_hi, ceiling - raw_lo
                ub = ceiling
                rc = -raw_rc
            else:
                val = raw_val
                lo, hi = raw_lo, raw_hi
                ub = raw_ub
                rc = raw_rc
            rc_kwh[t] = rc
            degenerate[t] = is_degenerate
            if ub < _INTENT_TOL:
                # This period's own ceiling is itself ~0 -- nothing real
                # to cap. UNLIMIT when the plan would genuinely take
                # MORE if it could (a negative reduced cost means
                # relaxing that bound would improve the objective);
                # otherwise the honest limit really is 0 (both band
                # edges collapse to val, which is itself ~0 here).
                is_unlimit = rc < -_INTENT_TOL
            else:
                is_unlimit = abs(rc) <= _INTENT_TOL
            if is_unlimit:
                band_min[t] = lo
                band_max[t] = hi
                intent.append("UNLIMIT")
            else:
                band_min[t] = val
                band_max[t] = val
                intent.append("SET")
        return LoadSignals(
            name=name,
            band_min_kw=band_min,
            band_max_kw=band_max,
            reduced_cost_per_kwh=rc_kwh,
            intent=intent,
            degenerate=degenerate,
        )

    plan_load_signals: list[LoadSignals] = []
    if result.ranging_valid:
        plan_load_signals = [
            _load_signal(
                sl.name,
                shed_vars[sl.name],
                load_ub[sl.name],
                mirror_against=effective_shed_forecast[sl.name],
            )
            for sl in sheddable_loads
        ] + [
            _load_signal(al.name, adequacy_vars[al.name], load_ub[al.name])
            for al in adequacy_loads
        ]

    # nimbus issue #494 (Signals 5/7 of #489): the period-0 offer-curve
    # price sweep, opt-in via compute_offer_curve -- see build_plan()'s
    # own docstring for the full mechanism/reasoning. `keep_basis=
    # compute_offer_curve` above (the p.solve() call) is what makes
    # result.sweep_cost() available at all here; still guarded on
    # `compute_offer_curve` explicitly (not just "does the LPResult
    # happen to have a live basis") so this block reads as a plain,
    # self-contained opt-in the same way the ranging block above does.
    if compute_offer_curve:
        _offer_curve_start = time.monotonic()
        hours0 = float(hours[0])
        # nimbus issue #733 (Mark Purcell, live finding, real household
        # install, both EV participants): sweep_cost_with_ranging()
        # warm-starts from `result`'s own already-solved HiGHS basis and
        # only ever changes ONE variable's cost coefficient
        # (`h.changeColCost`) before re-solving. When `result` came from
        # a CalibratedOptions (or Blended/Lex) solve, every OTHER
        # variable in that basis still carries its own `primary +
        # calibrated_weight * secondary` blended cost -- calibrated
        # specifically against the REAL, near-baseline price this cycle
        # actually solved at. Sweeping one variable's cost out to an
        # extreme domain edge (the Market Floor/Cap, nowhere near the
        # real price the calibration was computed for) makes that one
        # variable's incentive dominate the objective in a way the
        # calibration weight was never chosen to handle, while every
        # other variable's stale secondary weighting still resists
        # moving away from its own calibrated position -- confirmed live
        # and reproduced in a minimal synthetic scenario (offer curve
        # reports ~0 kW import at -$1/kWh; a genuine fresh re-solve at
        # that identical price correctly wants the full charge/import
        # envelope). A real, fresh CalibratedOptions re-solve at each
        # swept price (Mark's own tested-correct fix) would need a full
        # lex+calibration search per breakpoint -- `_calibrate_blend_
        # weight()`'s own up-to-40-step binary search, times up to
        # `_OFFER_CURVE_MAX_BREAKPOINTS` steps per curve -- a real,
        # unmeasured cost risk against this project's own "total sweep
        # time under 0.5s" bar (#494) at production scale.
        #
        # This solves it structurally instead: the offer curve's own
        # question -- "what would the LP economically choose at this
        # hypothetical price" -- doesn't need the secondary/calibration
        # machinery at all. That machinery exists purely to break ties
        # among otherwise-equally-PRIMARY-optimal solutions for the REAL
        # dispatch decision; it was never meant to reflect genuine
        # economic value, which is exactly what a demand-response offer
        # curve is supposed to communicate. So the walk (and the
        # always-swept retail point) run against a SEPARATE, plain
        # (`options=None`, secondary never read -- byte-identical to
        # this module's own pre-#696 default) solve of the exact same
        # problem `p`, instead of `result`. Only built when the real
        # dispatch solve actually engaged secondary costs in the first
        # place (`_use_secondary_costs`) -- a plain-mode `result` is
        # already the correct, zero-extra-cost base for every other
        # install, exactly as before this fix.
        _offer_curve_base_result = (
            p.solve(keep_basis=True) if _use_secondary_costs else result
        )
        if _offer_curve_base_result.status != "optimal":
            # Honest fail-open: an already-optimal `result` proves the
            # real problem IS feasible, so a plain re-solve of the exact
            # same `p` failing here would be a genuine anomaly, not an
            # expected outcome -- fall back to `result` itself (the
            # pre-#733 behaviour) rather than let a real edge case here
            # take down the whole plan.
            _LOGGER.warning(
                "Nimbus offer curve: plain-mode base re-solve for the "
                "#733 fix did not reach optimal (status=%s) despite the "
                "real calibrated solve succeeding -- falling back to the "
                "calibrated basis for this cycle's curve (pre-#733 "
                "behaviour, may reproduce that issue's own symptom this "
                "cycle only)",
                _offer_curve_base_result.status,
            )
            _offer_curve_base_result = result
        # nimbus issue #678: walks each curve's own real breakpoints
        # directly instead of sampling #494's original fixed 7-point
        # grid (_offer_curve_price_grid(), removed by this issue) -- see
        # _offer_curve_ranging_walk()'s own docstring for the full
        # mechanism, including its answers to #678's own open questions
        # (the epsilon nudge past a boundary, the iteration safety cap)
        # and #705's own two additions (walking each side from whichever
        # end its real structure clusters near, plus the gap-targeted
        # backstop solve).
        offer_curve_import: list[tuple[float, float]] | None
        offer_curve_import_ranging: list[tuple[float, float] | None] | None
        offer_curve_import, offer_curve_import_ranging = _offer_curve_ranging_walk(
            _offer_curve_base_result,
            grid_import[0],
            # start/ascending default to the floor, walking up -- import's
            # real breakpoints cluster near the floor (nimbus issue #705).
            retail=float(effective_import_price[0]),
            hours0=hours0,
            negated=False,
        )
        # Export earns revenue -- p.set_cost(grid_export[t], -price*hours[t])
        # above (the same construction this walk must mirror exactly for
        # the at-retail consistency check to hold), so negated=True here
        # matches that same sign flip; _offer_curve_ranging_walk() folds
        # the negation into what it swaps and _offer_curve_price_interval()
        # undoes it again on the way back out.
        offer_curve_export: list[tuple[float, float]] | None
        offer_curve_export_ranging: list[tuple[float, float] | None] | None
        offer_curve_export, offer_curve_export_ranging = _offer_curve_ranging_walk(
            _offer_curve_base_result,
            grid_export[0],
            # nimbus issue #705: export's real breakpoints cluster near
            # the CAP, not the floor -- walk from _OFFER_CURVE_DOMAIN_MAX
            # downward instead of #678's original uniform floor-upward
            # start, confirmed against real live household data (see this
            # walk's own docstring for the full before/after evidence).
            start=_OFFER_CURVE_DOMAIN_MAX,
            ascending=False,
            retail=float(effective_export_price[0]),
            hours0=hours0,
            negated=True,
        )
        offer_curve_sweep_seconds: float | None = time.monotonic() - _offer_curve_start
        _LOGGER.debug(
            "Nimbus network.py: offer curve sweep took %.4fs (%d import + %d export steps)",
            offer_curve_sweep_seconds,
            len(offer_curve_import),
            len(offer_curve_export),
        )
    else:
        offer_curve_import = None
        offer_curve_export = None
        offer_curve_import_ranging = None
        offer_curve_export_ranging = None
        offer_curve_sweep_seconds = None

    solar_used_arr = _get(solar_used)
    grid_import_excess_arr = _get(grid_import_excess)
    # mypy issue #384: export_bonus is list[str] | None -- restructured
    # out of the return statement's own inline ternary (which doesn't
    # narrow) into an explicit if/else on a local, same fix pattern as
    # the earlier has_export_bonus blocks above.
    if has_export_bonus:
        assert export_bonus is not None
        export_bonus_arr = _get(export_bonus)
    else:
        export_bonus_arr = np.zeros(n)
    return Plan(
        status="optimal",
        periods=periods,
        battery_charge_kw=battery_charge_kw_total,
        battery_discharge_kw=battery_discharge_kw_total,
        battery_soc_kwh=battery_soc_kwh_total,
        # Total real draw -- see this field's own docstring on Plan.
        # astype(float64): same numpy-stub dtype-widening note as
        # SheddableLoadPlan.served_kw above.
        grid_import_kw=(_get(grid_import) + grid_import_excess_arr).astype(np.float64),
        grid_export_kw=_get(grid_export),
        export_bonus_kw=export_bonus_arr,
        solar_used_kw=solar_used_arr,
        solar_curtailed_kw=(solar.forecast_kw - solar_used_arr).astype(np.float64),
        sheddable_loads=plan_sheddable,
        adequacy_loads=plan_adequacy,
        total_cost=result.objective,
        iterations=result.iterations,
        duals=result.duals,
        reduced_costs=result.reduced_costs,
        grid_import_excess_kw=grid_import_excess_arr,
        effective_solar_kw=np.asarray(effective_solar_kw, dtype=np.float64),
        effective_import_price=np.asarray(effective_import_price, dtype=np.float64),
        effective_export_price=np.asarray(effective_export_price, dtype=np.float64),
        batteries=plan_batteries,
        grid_signals=grid_signals,
        battery_signals=plan_battery_signals,
        load_signals=plan_load_signals,
        offer_curve_import=offer_curve_import,
        offer_curve_export=offer_curve_export,
        offer_curve_import_ranging=offer_curve_import_ranging,
        offer_curve_export_ranging=offer_curve_export_ranging,
        offer_curve_sweep_seconds=offer_curve_sweep_seconds,
    )
