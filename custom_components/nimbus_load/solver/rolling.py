"""Layer 2 -- Rolling Refinement (receding-horizon re-solve loop).

Layer 1 (network.py's own build_plan()) solves ONE horizon once. Nothing
in this codebase before this file ever called it more than once with
`previous_plan` threaded through -- meaning the three stability
mechanisms added in the previous commit (proximal regularization, rate
limiting, confidence-aware dispatch) had nothing to actually stabilize
AGAINST. This module is that loop: standard receding-horizon control
(the same "solve, act, observe, re-solve" pattern real MPC controllers
use) -- re-solve on a real cadence, with the window shifting forward
each time, always threading the previous solve's own plan through.

Deliberately, like every other module in this package, this NEVER writes
anything anywhere -- no Modbus, no HA entity, no automation trigger.
run_rolling_refinement() is a pure function: a config + a caller-supplied
input-provider callable in, a RollingRefinementResult out. Still
genuinely dead code -- not imported by __init__.py/coordinator.py/
config_flow.py.

## The one real MPC design point worth being precise about

In receding-horizon control, only PERIOD 0 of each individual re-solve
is ever actually acted on -- everything the solve planned beyond period 0
is provisional, discarded, and re-planned fresh on the next tick (which
will have better, more current information by then). This module's real
output is therefore a DISPATCH TIMELINE built from each tick's own
period-0 decision, not the full multi-period plan from any single solve
-- `RollingTick.dispatched_*` are what a real live controller running
this loop would actually have done, tick by tick.

## SoC continuity is this module's own responsibility, not the caller's

Each re-solve needs a real, physically-consistent `initial_soc_kwh` --
whatever the battery ACTUALLY held after the previous tick's real
dispatch, not some static value the caller's own input-provider happens
to return. Rather than trust every future caller to get this right
themselves (an easy, silent mistake -- a stale initial_soc_kwh would
silently disconnect the rolling loop's own SoC math from what it just
told the battery to do), `run_rolling_refinement()` OVERRIDES whatever
`initial_soc_kwh` the input-provider's own BatteryConfig carries with the
real running SoC it tracked from the immediately-previous tick's own
dispatch (first tick only: whatever the provider itself supplies, since
there's no prior tick to inherit from).

The carried-forward SoC is CLAMPED into whatever `min_soc_kwh`/
`max_soc_kwh` the CURRENT tick's own BatteryConfig declares, not passed
through raw. This matters for real callers, not just theoretical safety:
this project's own real household already runs automations that shift
`min_soc`/`max_soc` policy ON A SCHEDULE during the day (see the sibling
116KAT-HA-AI repo's own CLAUDE.md, HAEO battery-cost-schedule
automations) -- an input-provider reflecting that is entirely
legitimate, not a caller bug, and a hard crash the moment the real
running SoC briefly sits outside a just-tightened bound would be overly
brittle. A real battery's actual stored energy doesn't jump just because
the ALLOWED usable window moved; clamping is the correct physical
interpretation of "where does this solve start from." The clamp only
affects what's fed INTO the next solve -- the true dispatched SoC for
every tick is still recorded exactly, unclamped, in that tick's own
`RollingTick.dispatched_soc_kwh`.

## Solve-failure fallback (a real, if partial, answer to architecture
sketch §8, "solve-failure fallback" -- not previously built anywhere)

If a re-solve comes back infeasible/unbounded, this loop does NOT invent
a plan or crash -- it carries the LAST KNOWN-GOOD dispatch forward
unchanged for that tick (a real, defensible fallback: freezing the
current setpoint is safer than either stopping entirely or guessing).
Two things this deliberately does NOT do, both real, honest gaps left
for later: (1) it does not retry with a relaxed config (e.g. a looser
rate limit) -- a caller wanting that layers it on top of this loop's own
result; (2) `previous_plan` fed into the NEXT re-solve is still the last
REAL optimal plan, never the failed solve's own zero-filled placeholder
(see network.py's `_infeasible_plan()`) -- using a zero-filled plan as a
continuity anchor would make proximal regularization/rate limiting
believe the previous real dispatch was zero everywhere, corrupting the
very next solve for no real reason.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SheddableLoadConfig, SolarConfig
from .network import DEFAULT_PROXIMAL_WEIGHT_KW, Plan, build_plan


@dataclass(frozen=True)
class RollingInputs:
    """Everything build_plan() needs for ONE re-solve, as of a specific
    real `now` instant -- returned fresh by the caller's own
    InputProvider on every tick, since real forecasts/prices genuinely
    change between re-solves (this is the whole reason a rolling loop
    exists rather than trusting one solve's own far-future periods
    forever). `periods.start` MUST be set to (or after) `now` -- see
    run_rolling_refinement()'s own validation.
    """

    periods: PeriodGrid
    grid: GridConfig
    battery: BatteryConfig
    solar: SolarConfig
    loads: list[LoadConfig] | None = None
    sheddable_loads: list[SheddableLoadConfig] | None = None


InputProvider = Callable[[datetime], RollingInputs]


@dataclass(frozen=True)
class RollingTick:
    """One re-solve's own real record: what it decided as a full plan
    (`plan.status` may be non-optimal -- always check before trusting
    `plan`'s own arrays, same convention as Plan itself), and what
    actually got DISPATCHED for this tick specifically (only ever period
    0 of `plan` when optimal; the previous tick's own dispatch, carried
    forward unchanged, when this solve failed -- see this module's own
    docstring for why).
    """

    solved_at: datetime
    plan: Plan
    dispatched_charge_kw: float
    dispatched_discharge_kw: float
    dispatched_grid_import_kw: float
    dispatched_grid_export_kw: float
    dispatched_soc_kwh: float


@dataclass(frozen=True)
class RollingRefinementResult:
    """The full record of a rolling-refinement run. `n_infeasible` is a
    real health signal on its own -- a nonzero count means the fallback
    (this module's own docstring) fired for at least one tick, worth a
    caller's attention even though the loop itself never crashes on it.
    """

    ticks: list[RollingTick]
    n_infeasible: int

    @property
    def dispatch_charge_kw(self) -> list[float]:
        return [t.dispatched_charge_kw for t in self.ticks]

    @property
    def dispatch_discharge_kw(self) -> list[float]:
        return [t.dispatched_discharge_kw for t in self.ticks]

    @property
    def dispatch_grid_import_kw(self) -> list[float]:
        return [t.dispatched_grid_import_kw for t in self.ticks]

    @property
    def dispatch_grid_export_kw(self) -> list[float]:
        return [t.dispatched_grid_export_kw for t in self.ticks]

    @property
    def dispatch_soc_kwh(self) -> list[float]:
        return [t.dispatched_soc_kwh for t in self.ticks]


@dataclass(frozen=True)
class RollingRefinementConfig:
    """`start`/`resolve_interval`/`n_resolves` define the tick schedule
    -- re-solve at `start`, `start + resolve_interval`, `start +
    2*resolve_interval`, ... for `n_resolves` total ticks. The other
    three fields are passed straight through to every build_plan() call,
    same meaning/defaults as build_plan()'s own (see network.py's
    module docstring) -- `proximal_weight`/`max_rate_kw` only do
    anything meaningful here (a one-shot Layer-1-only build_plan() call
    has no previous_plan to stabilize against at all; this loop is what
    actually creates one).
    """

    start: datetime
    resolve_interval: timedelta
    n_resolves: int
    proximal_weight: float = DEFAULT_PROXIMAL_WEIGHT_KW
    max_rate_kw: float | None = None
    risk_aversion: float = 0.0

    def __post_init__(self) -> None:
        if self.n_resolves <= 0:
            msg = "n_resolves must be positive"
            raise ValueError(msg)
        if self.resolve_interval.total_seconds() <= 0:
            msg = "resolve_interval must be positive"
            raise ValueError(msg)


def run_rolling_refinement(config: RollingRefinementConfig, input_provider: InputProvider) -> RollingRefinementResult:
    """Run the receding-horizon loop. Pure function -- no I/O, no HA
    dependency, safe to call from anywhere including a plain local test
    script (same guarantee as build_plan() itself).
    """
    ticks: list[RollingTick] = []
    previous_plan: Plan | None = None
    last_dispatch: tuple[float, float, float, float, float] | None = None
    n_infeasible = 0
    now = config.start

    for _ in range(config.n_resolves):
        inputs = input_provider(now)
        if inputs.periods.start is None:
            msg = "RollingInputs.periods.start must be set -- cross-solve alignment (see network.py) is impossible without a real calendar anchor"
            raise ValueError(msg)
        if inputs.periods.start < now:
            msg = f"RollingInputs.periods.start ({inputs.periods.start}) is before this tick's own 'now' ({now}) -- the input provider must return a window starting at or after 'now'"
            raise ValueError(msg)

        # SoC continuity is THIS loop's own responsibility, not the input
        # provider's -- see module docstring, including why the carried
        # value is CLAMPED into this tick's own bounds rather than
        # passed through raw (a real min_soc/max_soc policy shift
        # between ticks is legitimate, not a caller bug).
        battery = inputs.battery
        if last_dispatch is not None:
            running_soc = min(max(last_dispatch[4], battery.min_soc_kwh), battery.max_soc_kwh)
            battery = dataclasses.replace(battery, initial_soc_kwh=running_soc)

        plan = build_plan(
            periods=inputs.periods,
            grid=inputs.grid,
            battery=battery,
            solar=inputs.solar,
            loads=inputs.loads,
            sheddable_loads=inputs.sheddable_loads,
            previous_plan=previous_plan,
            proximal_weight=config.proximal_weight,
            max_rate_kw=config.max_rate_kw,
            risk_aversion=config.risk_aversion,
        )

        if plan.is_optimal:
            dispatched = (
                float(plan.battery_charge_kw[0]),
                float(plan.battery_discharge_kw[0]),
                float(plan.grid_import_kw[0]),
                float(plan.grid_export_kw[0]),
                float(plan.battery_soc_kwh[0]),
            )
            previous_plan = plan
        else:
            n_infeasible += 1
            # Fallback: freeze the last known-good dispatch. First tick
            # ever failing has no prior dispatch to fall back to at all
            # -- honestly report zero/starting-SoC rather than invent one
            # (a real solve-failure RETRY strategy is a caller-level
            # concern, not built here -- see module docstring).
            dispatched = last_dispatch if last_dispatch is not None else (0.0, 0.0, 0.0, 0.0, battery.initial_soc_kwh)
            # previous_plan intentionally NOT updated to this failed
            # plan -- see module docstring.

        last_dispatch = dispatched
        ticks.append(
            RollingTick(
                solved_at=now,
                plan=plan,
                dispatched_charge_kw=dispatched[0],
                dispatched_discharge_kw=dispatched[1],
                dispatched_grid_import_kw=dispatched[2],
                dispatched_grid_export_kw=dispatched[3],
                dispatched_soc_kwh=dispatched[4],
            )
        )
        now = now + config.resolve_interval

    return RollingRefinementResult(ticks=ticks, n_infeasible=n_infeasible)
