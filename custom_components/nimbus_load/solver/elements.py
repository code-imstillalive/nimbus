"""Element configuration types for the Nimbus Solver's LP network.

Deliberately narrower than HAEO's own arbitrary-graph element model (see
this project's own architecture sketch, "01a Sheddable loads" and
"05 Deliberately out of scope for v1") -- this models this ONE household's
real topology: one grid connection, one aggregate battery (this
household's 2 real inverters/4 towers are NOT modeled separately in v1,
matching the sketch's own stated simplification), one solar input, and
N loads (a subset of which may be SheddableLoad instead of plain Load).

Every element here is a pure dataclass -- no LP variables, no solver
references. network.py is the only place that turns these configs into
actual LPProblem variables/constraints. This separation exists so a
config can be constructed, validated, and inspected (e.g. in a test)
without ever touching the solver -- the same "config vs. execution"
split this project's own coordinator.py/ml/model.py split already
proved out for the Forecaster.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray

# Structural floor on charge/discharge cost (§3 of the architecture
# sketch: "Refusing the wash-trade bug structurally"). HAEO's own
# real, documented degeneracy: a 100%-efficient, zero-friction battery
# makes simultaneous charge and discharge cost-neutral, so a linear
# solver will genuinely find and exploit that loop. HAEO's own fix was a
# manually-tuned, reactive cost spread -- real, but re-breakable by
# anyone who sets either cost back to zero later. This floor makes that
# configuration impossible to express at all, not just inadvisable.
MIN_CHARGE_DISCHARGE_COST_SPREAD: float = 0.01  # $/kWh

# Same reasoning, same mechanism, applied to grid import/export -- a
# genuinely free simultaneous import+export is the identical degeneracy
# one hop over. Real meters can't do this in practice, but the LP has no
# way to know that without either an economic cost separation (this) or
# a binary mutual-exclusion constraint (deliberately not used -- see the
# sketch's own §7 "Two concrete improvements" discussion of why MILP is
# a real tradeoff, not an obvious win, for a network that re-solves this
# often).
MIN_GRID_COST_SPREAD: float = 0.001  # $/kWh


class DegenerateConfigError(ValueError):
    """Raised when a config would reintroduce the wash-trade degeneracy --
    deliberately a distinct exception type from a plain ValueError, so a
    caller (or a test) can specifically assert "this exact class of
    mistake is structurally impossible" rather than just "some ValueError
    was raised for some reason."
    """


@dataclass(frozen=True)
class PeriodGrid:
    """The time axis every element's per-period arrays are indexed against.
    `hours` gives each period's own duration in hours (variable-width
    periods are supported -- e.g. a coarser Daily Plan horizon vs. a
    fine-grained Rolling Refinement window, per the architecture sketch's
    own §2 layering), matching HAEO's own real Network class convention
    (`periods` in hours, confirmed directly from its source).

    `start`: real wall-clock time of this grid's own first period, or
    `None` if this grid has no real calendar anchor at all (e.g. a plain
    synthetic/relative-time test). This is what lets `build_plan()`
    genuinely align a NEW solve's periods against a PREVIOUS solve's
    periods by real elapsed time -- required for cross-solve continuity
    (plan stability / rate limiting, see network.py's own docstring) to
    mean anything at all. A rolling re-solve's new grid does NOT start at
    the same wall-clock instant as the previous solve's grid (time has
    genuinely moved on between solves) -- matching by array INDEX alone
    would silently compare the wrong periods to each other the moment the
    two grids' start times diverge, which is every single re-solve after
    the first. `start=None` is a deliberate, honest "no alignment
    possible" state, not an error -- every existing caller (every test
    predating this field) constructs a PeriodGrid with no calendar
    context at all, and continues to work unchanged; cross-solve
    continuity mechanisms simply have nothing to align against and are
    skipped, exactly as if no previous_plan had been passed at all.
    """

    hours: NDArray[np.float64]
    start: datetime | None = None

    @property
    def n_periods(self) -> int:
        return len(self.hours)

    @property
    def period_starts(self) -> list[datetime] | None:
        """Real wall-clock start time of every period, or None if this
        grid has no calendar anchor (`start is None`). Computed by
        cumulative addition of each period's own duration -- NOT assumed
        uniform, since `hours` explicitly supports variable-width periods.
        """
        if self.start is None:
            return None
        starts: list[datetime] = []
        t = self.start
        for h in self.hours:
            starts.append(t)
            t = t + timedelta(hours=float(h))
        return starts

    def __post_init__(self) -> None:
        if self.n_periods == 0:
            msg = "PeriodGrid must have at least one period"
            raise ValueError(msg)
        if np.any(self.hours <= 0):
            msg = "Every period duration must be strictly positive"
            raise ValueError(msg)


@dataclass(frozen=True)
class GridConfig:
    """The single grid connection point.

    import_price/export_price: $/kWh per period, already the final
    blended price (§5 of the architecture sketch -- Amber/LocalVolts/P2P
    blending happens BEFORE this config is built, this class only ever
    sees the final number).
    """

    import_price: NDArray[np.float64]
    export_price: NDArray[np.float64]
    import_limit_kw: float
    export_limit_kw: float
    # min_export_kwh (2026-08-17, direct response to real regret/EPR
    # analysis needing it -- see network.py's own docstring, "MINIMUM
    # TOTAL EXPORT COMMITMENT"): a real household running a P2P-style
    # export program (a FIXED, pre-committed volume, matched against
    # historical pattern rather than reactive per-interval pricing --
    # NOT a plain price-taking market) needs a way to force a
    # counterfactual dispatch (a perfect-foresight oracle, in
    # particular) to ALSO physically deliver that same real committed
    # volume, not just collect the credit for it. None (default) is a
    # complete no-op -- every existing caller, every test predating this
    # field, is byte-identical to before it existed.
    min_export_kwh: float | None = None
    # export_bonus_price / export_bonus_volume_kwh (2026-08-17, direct
    # response to a real, confirmed household finding: real P2P revenue
    # is NOT correctly modeled as a flat per-kWh price discount, e.g.
    # `match_fraction * p2p_rate + (1-match_fraction) * spot_rate`
    # applied UNIFORMLY to every exported kWh -- that dilutes the price
    # every single kWh appears to earn, which systematically undervalues
    # continuing to discharge late in a real P2P window (the household's
    # own direct, live report: "if the solver was good it would have
    # kept selling rather than landing prematurely"). The real mechanism
    # is closer to a FIXED ABSOLUTE nightly volume (documented
    # extensively in the sibling 116KAT-HA-AI repo's own CLAUDE.md --
    # LocalVolts matches against the household's known historical
    # pattern, not a per-kWh lottery): roughly the first N kWh of real
    # export each night get close to the REAL, undiluted achieved rate;
    # anything beyond that reverts to the much lower real spot rate.
    #
    # export_bonus_price[t]: the real INCREMENTAL premium (P2P rate minus
    # base/spot export_price[t]) available at period t, if any -- 0
    # outside a real P2P-style window. export_bonus_volume_kwh: the real
    # kWh eligible for that premium, PER REAL CALENDAR DAY (see
    # network.py's own docstring for a real bug this exact distinction
    # fixed -- a single WHOLE-HORIZON cap lets a multi-day solve greedily
    # front-load its entire bonus allocation into the very first night it
    # sees, then behave as if permanently exhausted for every later
    # night, which is NOT how real nightly-resetting P2P settlement
    # works). Both None (the default) is a complete no-op -- every
    # existing caller, every test predating this pair, is byte-identical
    # to before they existed.
    #
    # Mechanically (see network.py's own docstring for the LP detail):
    # grid_export[t] itself is completely unchanged (still the single,
    # real total export variable used everywhere -- balance equation,
    # wash-trade guards, stability mechanisms, reporting). A SEPARATE
    # export_bonus[t] variable, bounded by grid_export[t] (can't claim
    # bonus volume exceeding that period's real total export) and by one
    # cumulative constraint PER REAL CALENDAR DAY (sum(export_bonus[t]*
    # hours[t]) <= export_bonus_volume_kwh, for that day's own periods),
    # earns an EXTRA revenue credit of export_bonus_price[t] on top of
    # whatever grid_export[t] already earns at the base export_price[t]
    # rate. Since claiming bonus volume is strictly free money whenever
    # export_bonus_price[t] > 0, a revenue-maximizing LP always claims as
    # much of EACH day's own capped bonus allocation as it can, choosing
    # WHICH real periods WITHIN that day to claim it in based on real
    # economics (not a crude, arbitrary per-period split) -- exactly
    # reproducing "the first ~N kWh of real export EACH NIGHT get close
    # to the true achieved rate, everything beyond that reverts to
    # base/spot, resetting fresh the next real day."
    export_bonus_price: NDArray[np.float64] | None = None
    export_bonus_volume_kwh: float | None = None

    def __post_init__(self) -> None:
        if self.import_limit_kw < 0 or self.export_limit_kw < 0:
            msg = "Grid import/export limits must be >= 0"
            raise ValueError(msg)
        if self.min_export_kwh is not None and self.min_export_kwh < 0:
            msg = "min_export_kwh must be >= 0 when given"
            raise ValueError(msg)
        if (self.export_bonus_price is None) != (self.export_bonus_volume_kwh is None):
            msg = "export_bonus_price and export_bonus_volume_kwh must be given together, or not at all"
            raise ValueError(msg)
        if self.export_bonus_volume_kwh is not None and self.export_bonus_volume_kwh < 0:
            msg = "export_bonus_volume_kwh must be >= 0 when given"
            raise ValueError(msg)
        if self.export_bonus_price is not None and len(self.export_bonus_price) != len(self.export_price):
            msg = "export_bonus_price must have the same length as export_price"
            raise ValueError(msg)
        # REMOVED (2026-08-16): the price-spread config-time REJECT that
        # used to live here (import_price - export_price >= MIN_GRID_COST_
        # SPREAD everywhere, else raise DegenerateConfigError). Found to
        # be both too blunt AND not actually the right layer to fix this
        # at, via a real household's own live data: a genuine P2P sell
        # price legitimately, routinely exceeds import price during the
        # real 5pm-midnight window (that's the entire economic point of
        # selling P2P) -- this guard rejected that outright, and even a
        # per-period CLAMP workaround (tried first) neutered the real
        # price signal down near import-price levels, which is exactly
        # why an early build of this solver proposed almost no discharge
        # into a real household's own P2P window despite being fed the
        # real $0.50/kWh signal.
        #
        # The genuine risk this guard existed to prevent (the LP finding
        # a free-money "import cheap, instantly resell high, same
        # period" loop) is now closed STRUCTURALLY in network.py instead,
        # via two real physical constraints, not a price-based reject:
        #   1. grid_export[t] <= solar_used[t] + discharge[t] -- export
        #      can only be funded by real solar surplus or genuine
        #      battery discharge, NEVER directly by a same-period grid
        #      import (a real physical fact: a household meter reports
        #      one NET flow per interval, never simultaneous gross
        #      import+export).
        #   2. discharge[t] is bounded by the SoC that genuinely existed
        #      BEFORE period t (not inflated by that same period's own
        #      charge[t]) -- closes the second pathway (charge cheap,
        #      instantly discharge+export the same energy, same period),
        #      which constraint 1 alone does not block.
        # Together these make ANY same-period import-to-export round
        # trip physically infeasible, regardless of price, while leaving
        # genuine ACROSS-TIME arbitrage (charge cheap overnight in one
        # period, discharge to sell high in a LATER period) completely
        # untouched -- exactly the real, legitimate behaviour this
        # config exists to let the LP discover.
        #
        # MIN_GRID_COST_SPREAD (module-level constant, kept for the
        # battery's own analogous MIN_CHARGE_DISCHARGE_COST_SPREAD
        # reasoning) is no longer referenced here -- see network.py's
        # own docstring, "SAME-PERIOD WASH-TRADE PREVENTION", for the
        # full replacement mechanism and why it's structurally sufficient
        # on its own without also needing a price-based reject.


@dataclass(frozen=True)
class BatteryConfig:
    """The single aggregate battery (v1 simplification -- see module
    docstring). Charge/discharge modeled as two separate nonnegative
    variables in network.py, never one signed free variable, specifically
    so each can carry its own independently-enforced cost floor.

    salvage_value / headroom_value (2026-08-16, direct response to real
    feedback -- Mark Purcell, on three of his own four reported failure
    scenarios: "Failure to price the forward option value of stored
    energy AND of storage headroom. Terminal-value problem in the
    optimisation layer... Given the horizon is already four days, the
    forward value function is the live issue."):

    `salvage_value` alone (the only terminal-value term that existed
    before this field) prices ENERGY remaining at the final period --
    it gives the LP a reason not to drain the battery for no reason, but
    gives it ZERO reason to prefer ending with UNUSED CAPACITY, i.e. it
    has no concept of the real option value in being positioned to
    absorb a future cheap/negative price that lies just past whatever
    horizon this particular solve can see. `headroom_value` is the
    symmetric term for that: $/kWh credited for `max_soc_kwh -
    soc[final]` (unused capacity) at the final period, exactly mirroring
    salvage_value's own treatment of remaining energy. Having BOTH
    terms, not just one, is what lets a solve express "end somewhere in
    the genuine middle, with real optionality in both directions" rather
    than being structurally pulled toward one extreme (fully charged, or
    fully able to charge) by construction -- real options-theory
    reasoning, not a heuristic tiebreaker.

    Both default such that omitting `headroom_value` (0.0) is byte-
    identical to every scenario built before this field existed --
    salvage_value alone, same as always.

    **Real, honest limitation, found via real household data, not
    assumed**: this term is LINEAR in soc[final], so the LP always
    drives the terminal state to a hard CORNER (max_soc or min_soc) once
    one credit exceeds the other -- confirmed live: on a real 6h window,
    headroom_value=0.05 (< salvage_value=0.10) gave final_soc=100%,
    headroom_value=0.15 (> salvage_value=0.10) flipped ALL THE WAY to
    final_soc=5%, with no smooth transition in between. This mechanism
    is real and useful (it does let a caller bias the terminal state
    toward energy-preference or headroom-preference, predictably), but
    it is NOT a genuine continuous option-value tradeoff -- a real
    concave/nonlinear terminal value function (or a piecewise-linear
    approximation with multiple breakpoints) would be needed for that,
    and is a real, larger, not-yet-built follow-up, not something this
    simple linear credit already solves.
    """

    capacity_kwh: float
    initial_soc_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float  # 0 < eff <= 1, energy INTO storage per kWh drawn
    discharge_efficiency: float  # 0 < eff <= 1, energy delivered per kWh drawn from storage
    # $/kWh, structural floor enforced below. Either a scalar (applied
    # identically to every period, the original behaviour) OR a real
    # per-period array (2026-08-16, direct real finding: this household's
    # OWN deployed automations vary discharge_cost by time of day --
    # 0.01 5pm-7am, 0.09 7am-5pm, confirmed live from automations.yaml --
    # a caller feeding a single flat scalar for a multi-day horizon was
    # using whatever value happened to be live AT SOLVE TIME for the
    # entire horizon, including hours where the real system uses a very
    # different value. Confirmed live this was the actual root cause of
    # a real household reporting the LP going idle overnight instead of
    # discharging to serve load -- at the WRONG flat 0.09 (the daytime
    # value), discharging looked far less obviously favourable than it
    # really is at the REAL overnight 0.01.
    charge_cost: float | NDArray[np.float64]
    discharge_cost: float | NDArray[np.float64]
    salvage_value: float  # $/kWh credited for ENERGY remaining at the final period
    headroom_value: float = 0.0  # $/kWh credited for unused CAPACITY (max_soc - soc[final]) at the final period -- see docstring above
    # Real piecewise-linear concave terminal value (2026-08-18, the
    # "not-yet-built follow-up" this class's own docstring names above
    # -- Mark Purcell's audit item #7). A list of (width_kwh, rate_per_
    # kwh) segments describing the SoC range ABOVE min_soc_kwh, each
    # with its own credit rate -- e.g. [(10.0, 0.60), (60.0, 0.30),
    # (20.0, 0.10)] values the first 10kWh of reserve above the floor at
    # $0.60/kWh, the next 60kWh at $0.30/kWh, the final 20kWh (nearest
    # max_soc) at only $0.10/kWh -- a genuine diminishing-marginal-value
    # curve, replacing the existing flat salvage_value/headroom_value
    # mechanism's single rate across the WHOLE range (see this class's
    # own docstring above for the real, confirmed "hard corner" problem
    # that single flat rate causes).
    #
    # None (the default) is fully backward compatible -- falls through
    # to the exact existing salvage_value/headroom_value flat-rate
    # behaviour, byte-identical to every scenario built before this
    # field existed. When provided, REPLACES (not adds to) salvage_
    # value/headroom_value entirely for that solve -- the two mechanisms
    # model the same real thing (terminal-state value) at different
    # levels of fidelity, using both together would double-count.
    #
    # Segment widths must sum to exactly (max_soc_kwh - min_soc_kwh) --
    # the full above-floor range, nothing left unpriced and nothing
    # double-priced. Rates must be non-increasing (segment i's rate >=
    # segment i+1's rate) -- this is what makes the LP construction
    # (network.py's own segment-fill variables, one per breakpoint,
    # summing to soc[final] - min_soc_kwh) actually behave concavely: an
    # LP minimizing cost (maximizing credit) will always fill the
    # highest-rate segment first for any given amount of energy to
    # allocate, purely from its own optimization pressure, no explicit
    # ordering constraint needed -- but ONLY if rates are genuinely
    # non-increasing; an increasing sequence would let the LP exploit it
    # by skipping straight to a later segment, which is not what a
    # concave value function means and would be a real modeling bug.
    terminal_value_breakpoints: list[tuple[float, float]] | None = None

    def __post_init__(self) -> None:
        if not (0.0 < self.min_soc_kwh <= self.max_soc_kwh <= self.capacity_kwh):
            msg = f"Invalid SoC bounds: 0 < min_soc({self.min_soc_kwh}) <= max_soc({self.max_soc_kwh}) <= capacity({self.capacity_kwh}) required"
            raise ValueError(msg)
        if not (self.min_soc_kwh <= self.initial_soc_kwh <= self.max_soc_kwh):
            msg = f"initial_soc_kwh ({self.initial_soc_kwh}) must be within [min_soc, max_soc]"
            raise ValueError(msg)
        # Strict < 1.0 on BOTH sides, not <= -- exactly 100% must be
        # REJECTED, not merely allowed at the boundary (real bug caught
        # by test_network_synthetic.py: an earlier version used <= 1.0,
        # which let exactly 100% silently pass validation, directly
        # contradicting the architecture sketch's own explicit call:
        # "100% is rejected as a config value, not just discouraged").
        if not (0.0 < self.charge_efficiency < 1.0) or not (0.0 < self.discharge_efficiency < 1.0):
            msg = "Battery efficiencies must be in (0, 1] -- exactly 100% is rejected (see the architecture sketch's own §6: real efficiency is also a natural degeneracy guard, independent of the cost floor)"
            raise DegenerateConfigError(msg)
        # np.asarray + elementwise comparison handles BOTH a plain scalar
        # (0-d array, np.any() over a single value works fine) and a real
        # per-period array uniformly -- every period's own cost pair must
        # individually clear the floor, not just their average/sum.
        spread = np.asarray(self.charge_cost) + np.asarray(self.discharge_cost)
        if np.any(spread < MIN_CHARGE_DISCHARGE_COST_SPREAD):
            bad = spread if spread.ndim == 0 else spread[spread < MIN_CHARGE_DISCHARGE_COST_SPREAD]
            msg = (
                f"Battery charge_cost + discharge_cost ({bad}) is below the "
                f"structural minimum ({MIN_CHARGE_DISCHARGE_COST_SPREAD}) -- this "
                "is the exact configuration that produced HAEO's own documented "
                "wash-trade degeneracy"
            )
            raise DegenerateConfigError(msg)
        if self.terminal_value_breakpoints is not None:
            widths = [w for w, _rate in self.terminal_value_breakpoints]
            rates = [r for _w, r in self.terminal_value_breakpoints]
            if any(w <= 0 for w in widths):
                msg = f"terminal_value_breakpoints: every segment width must be > 0 (got {widths})"
                raise ValueError(msg)
            total_width = sum(widths)
            expected_width = self.max_soc_kwh - self.min_soc_kwh
            if abs(total_width - expected_width) > 1e-6:
                msg = (
                    f"terminal_value_breakpoints: segment widths must sum to exactly "
                    f"max_soc_kwh - min_soc_kwh ({expected_width}), got {total_width} -- "
                    "every kWh above the floor must be priced exactly once, no gaps, no overlap"
                )
                raise ValueError(msg)
            if any(rates[i] < rates[i + 1] for i in range(len(rates) - 1)):
                msg = (
                    f"terminal_value_breakpoints rates must be non-increasing "
                    f"(got {rates}) -- an increasing sequence is not a concave "
                    "value function and would let the LP exploit it by skipping "
                    "straight to a later, higher-rate segment"
                )
                raise DegenerateConfigError(msg)


def _validate_confidence_band(
    label: str, forecast_kw: NDArray[np.float64], lower_kw: NDArray[np.float64] | None, upper_kw: NDArray[np.float64] | None
) -> None:
    """Shared validation for the optional lower_kw/upper_kw confidence
    band any forecast-bearing element may carry (see CONFIDENCE-AWARE
    DISPATCH in network.py's own docstring for how these get used). Both
    must be given together or not at all -- a one-sided band has no
    honest interpretation for either the load-side (pessimistic = high)
    or solar-side (pessimistic = low) risk adjustment network.py applies.
    """
    if (lower_kw is None) != (upper_kw is None):
        msg = f"{label}: lower_kw and upper_kw must both be given, or neither (got one without the other)"
        raise ValueError(msg)
    if lower_kw is None:
        return
    n = len(forecast_kw)
    if len(lower_kw) != n or len(upper_kw) != n:
        msg = f"{label}: lower_kw/upper_kw must have {n} periods each, matching forecast_kw"
        raise ValueError(msg)
    if np.any(lower_kw > forecast_kw + 1e-9) or np.any(upper_kw < forecast_kw - 1e-9):
        msg = f"{label}: confidence band must satisfy lower_kw <= forecast_kw <= upper_kw at every period"
        raise ValueError(msg)


@dataclass(frozen=True)
class SolarConfig:
    """Real forecast, in kW per period -- from Nimbus's own already-
    validated Forecaster (Nimbus Solar power_signal), never a raw
    unvalidated source. Solar can always be curtailed DOWN from this
    forecast (a real decision the LP is free to make -- see HAEO's own
    excess-solar priority order this project already documented:
    load > battery > sheddables > curtailment > negative-price export,
    last resort), never forced to produce above it.

    lower_kw/upper_kw: optional, the Forecaster's own genuine model-
    derived confidence band for this same forecast (both None = no band
    available / not used, current behaviour, byte-identical to before
    this field existed). See network.py's CONFIDENCE-AWARE DISPATCH
    section for how `risk_aversion` turns this into an actually-used
    adjustment, not just decoration.
    """

    forecast_kw: NDArray[np.float64]
    lower_kw: NDArray[np.float64] | None = None
    upper_kw: NDArray[np.float64] | None = None

    def __post_init__(self) -> None:
        if np.any(self.forecast_kw < 0):
            msg = "Solar forecast cannot be negative"
            raise ValueError(msg)
        _validate_confidence_band("Solar", self.forecast_kw, self.lower_kw, self.upper_kw)


@dataclass(frozen=True)
class LoadConfig:
    """A plain, non-sheddable load -- served in full, every period, no
    exceptions. From Nimbus's own per-load Forecaster output.

    lower_kw/upper_kw: see SolarConfig's own docstring -- same optional
    confidence-band mechanism, same default of "not used" when absent.
    """

    name: str
    forecast_kw: NDArray[np.float64]
    lower_kw: NDArray[np.float64] | None = None
    upper_kw: NDArray[np.float64] | None = None

    def __post_init__(self) -> None:
        if np.any(self.forecast_kw < 0):
            msg = f"Load '{self.name}' forecast cannot be negative"
            raise ValueError(msg)
        _validate_confidence_band(f"Load '{self.name}'", self.forecast_kw, self.lower_kw, self.upper_kw)


@dataclass(frozen=True)
class SheddableLoadConfig:
    """A load that CAN be reduced below its own forecast, at a real cost.
    Modeled from day one per the architecture sketch's own §1a, even with
    zero real instances configured -- this class existing costs nothing;
    retrofitting it into an already-built LP formulation later would not.

    shed_cost: $/kWh, the LP's own price for reducing this load below its
    forecast. Deliberately high by default (see DEFAULT_SHED_COST) so
    shedding only happens under genuine economic/physical necessity
    (e.g. avoiding a binding grid import limit), never as a routine
    substitute for battery/grid dispatch -- this is NOT the same
    mechanism as this project's own real, working fixed-slot HWS
    automations, and is not intended to replace them (see the sketch's
    own explicit correction on this point).
    min_fraction: the smallest fraction of the forecast that MUST still
    be served (0.0 = can be shed to zero, 1.0 = cannot be shed at all --
    equivalent to a plain LoadConfig at that point, but keeping it in
    this class means it can be dialed back up/down without a config
    migration).
    lower_kw/upper_kw: see SolarConfig's own docstring -- same optional
    confidence-band mechanism.
    """

    name: str
    forecast_kw: NDArray[np.float64]
    shed_cost: float
    min_fraction: float = 0.0
    lower_kw: NDArray[np.float64] | None = None
    upper_kw: NDArray[np.float64] | None = None

    def __post_init__(self) -> None:
        if np.any(self.forecast_kw < 0):
            msg = f"Sheddable load '{self.name}' forecast cannot be negative"
            raise ValueError(msg)
        if not (0.0 <= self.min_fraction <= 1.0):
            msg = f"Sheddable load '{self.name}' min_fraction must be in [0, 1]"
            raise ValueError(msg)
        if self.shed_cost <= 0.0:
            msg = f"Sheddable load '{self.name}' shed_cost must be > 0 -- a zero/negative shed cost would make the LP shed this load for no real reason"
            raise ValueError(msg)
        _validate_confidence_band(f"Sheddable load '{self.name}'", self.forecast_kw, self.lower_kw, self.upper_kw)


# A real, deliberately conservative default -- higher than any realistic
# real-time energy price this project has ever recorded (this project's
# own history shows real spikes well under $1/kWh even during genuine
# scarcity events), so a SheddableLoadConfig created without an explicit
# shed_cost only ever gets shed as a true last resort, never routinely.
DEFAULT_SHED_COST: float = 2.00


@dataclass(frozen=True)
class AdequacyLoadConfig:
    """A load with a real DEADLINE, not a per-period demand -- direct
    response to real feedback (Mark Purcell, on his own scenario 2, hot
    water running cold / an EV short of range for a trip): "a missing
    constraint in J. If hot water adequacy and EV departure readiness
    are not in the objective, the solver is not making a mistake, it is
    correctly solving the wrong problem."

    Structurally different from both LoadConfig (served in full every
    period, zero flexibility) and SheddableLoadConfig (can be reduced
    below a FORECAST, per-period, still no deadline semantics at all --
    `min_fraction` is a per-period floor, not a cumulative-by-deadline
    target). This class has no forecast at all: the LP is free to
    deliver power to it at ANY level in [0, max_power_kw] during
    [earliest_period, deadline_period], at zero direct cost (running it
    earlier or later than some "expected" time costs nothing physically
    -- only failing to reach the real target by the real deadline does),
    constrained so the CUMULATIVE energy delivered by (and including)
    `deadline_period` is at least `target_kwh`. This is exactly the
    real, physical shape of HWS heating (must reach a target amount of
    stored heat by some time) and EV charging (must have enough range by
    departure) -- matches this project's own real, already-instrumented
    HWS/CTP telemetry Mark points to directly.

    A genuinely infeasible target (more energy required than
    `max_power_kw * (deadline_period - earliest_period + 1) * hours`
    can physically deliver) is NOT caught here at construction time --
    network.py doesn't know the real PeriodGrid's own `hours` until
    build_plan() is called, so an impossible target surfaces honestly as
    a real `status="infeasible"` Plan, same as any other genuinely
    unsatisfiable constraint in this solver, not a silently-adjusted or
    pre-emptively-rejected config.
    """

    name: str
    max_power_kw: float
    target_kwh: float
    deadline_period: int  # inclusive -- cumulative delivered energy through this period must reach target_kwh
    earliest_period: int = 0  # cannot deliver any power before this period (e.g. "don't run HWS before 6am")

    def __post_init__(self) -> None:
        if self.max_power_kw <= 0.0:
            msg = f"Adequacy load '{self.name}' max_power_kw must be > 0"
            raise ValueError(msg)
        if self.target_kwh <= 0.0:
            msg = f"Adequacy load '{self.name}' target_kwh must be > 0"
            raise ValueError(msg)
        if self.deadline_period < self.earliest_period:
            msg = f"Adequacy load '{self.name}' deadline_period ({self.deadline_period}) must be >= earliest_period ({self.earliest_period})"
            raise ValueError(msg)
        if self.earliest_period < 0:
            msg = f"Adequacy load '{self.name}' earliest_period must be >= 0"
            raise ValueError(msg)
