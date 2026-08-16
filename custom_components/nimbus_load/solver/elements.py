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

    def __post_init__(self) -> None:
        if self.import_limit_kw < 0 or self.export_limit_kw < 0:
            msg = "Grid import/export limits must be >= 0"
            raise ValueError(msg)
        # Structural degeneracy guard, same reasoning as the battery's
        # own (see MIN_GRID_COST_SPREAD docstring) -- a period where
        # import_price <= export_price makes simultaneous buy-and-sell
        # genuinely profitable, not just numerically degenerate. This is
        # deliberately NOT clamped/silently corrected -- a caller feeding
        # in a real inverted price (which can legitimately happen for a
        # single period during a genuine negative-FIT event) needs to see
        # this loudly, not have it silently papered over.
        spread = self.import_price - self.export_price
        if np.any(spread < MIN_GRID_COST_SPREAD):
            bad_periods = np.where(spread < MIN_GRID_COST_SPREAD)[0]
            msg = (
                f"Grid import/export price spread below the structural minimum "
                f"({MIN_GRID_COST_SPREAD}) at period(s) {bad_periods.tolist()} -- "
                "this would make simultaneous import+export profitable"
            )
            raise DegenerateConfigError(msg)


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
    charge_cost: float  # $/kWh, structural floor enforced below
    discharge_cost: float  # $/kWh, structural floor enforced below
    salvage_value: float  # $/kWh credited for ENERGY remaining at the final period
    headroom_value: float = 0.0  # $/kWh credited for unused CAPACITY (max_soc - soc[final]) at the final period -- see docstring above

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
        spread = self.charge_cost + self.discharge_cost
        if spread < MIN_CHARGE_DISCHARGE_COST_SPREAD:
            msg = (
                f"Battery charge_cost + discharge_cost ({spread}) is below the "
                f"structural minimum ({MIN_CHARGE_DISCHARGE_COST_SPREAD}) -- this "
                "is the exact configuration that produced HAEO's own documented "
                "wash-trade degeneracy"
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
