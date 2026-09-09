"""nimbus issue #479 (sub-issue 3/10 of the controllable-loads spec, #476):
per-load run-state -- currently_on/on_since/off_since/delivered_today_kwh/
carry_kwh/day_key -- the shared foundation #484 (monitoring's chatter-guard,
which needs persisted commanded_state/commanded_since to hysteresis-guard
relay calls across 5-minute re-solves) and #480 (early completion) both need
before they can be built correctly, plus #479's own daily quota carry/
rollover math ("pool pump 4h/day; missed hours roll into tomorrow").

Scope, honestly: this module is the state store and the pure rollover math
(tested against #479's own synthetic 3-day acceptance scenario), wired to
update from each Controllable Load's own (optional) real power sensor every
solve tick -- for EVERY configured kind, not just quota, since #484's
chatter-guard needs currently_on/on_since/off_since regardless of kind. It
does NOT add a new selectable "quota" wizard kind (#486 already reserved the
name in the data model without exposing it -- see docs/controllable-loads.md
-- and that stays true here); until a load can actually be configured as
quota-kind, effective_target_kwh()/remaining_kwh() have nothing real to
attach to yet, but are ready, tested, and waiting for #479's own wizard
follow-up.

Pure functions only below the two dataclasses -- no HA imports at module
level, so this resolves the same whether imported as part of the real
package (native mode) or as a bare top-level module (this project's own
test harness, and the standalone/cron deployment's own import shape,
even though LoadRunStateStore itself is never exercised there -- there is
no ConfigSubentries concept in standalone mode, same reasoning as
solver_writer.py's own build_controllable_loads()).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

# A load reading below this is "off" for on_since/off_since transition
# purposes -- real sensors rarely settle at an exact 0.0 (standby draw,
# CT-clamp noise), so a hard >0 threshold would flap constantly.
DEFAULT_ON_THRESHOLD_KW = 0.05

# Caps how large a single sample's elapsed-time*power energy credit can be,
# so a restart or a long sensor-unavailable gap (last_sample_at from hours
# ago) doesn't spuriously credit delivered_today_kwh with power*(a huge
# gap) that never actually happened. A normal solve tick is ~5 min; 1h is a
# generous margin for a missed tick or two, not an invitation to guess.
MAX_SAMPLE_GAP_HOURS = 1.0

# nimbus issue #484's own default hysteresis: "at least min_on_periods
# (sub-issue 2, #478 -- not yet built) or a default hysteresis of 2
# periods". Always used today since #478's own per-load override doesn't
# exist yet.
DEFAULT_MIN_HYSTERESIS_PERIODS = 2


@dataclass(frozen=True)
class LoadRunState:
    """One Controllable Load's own persisted run-state. Every field
    defaults to a genuine "never sampled yet" value -- LoadRunStateStore.
    async_read() returns exactly this for a load it has no entry for."""

    currently_on: bool = False
    on_since: float | None = None  # epoch seconds
    off_since: float | None = None  # epoch seconds
    delivered_today_kwh: float = 0.0
    carry_kwh: float = 0.0
    day_key: str = ""  # "" = never sampled; see apply_power_sample's own
    # day-key-change check, which treats "" as "no real yesterday to roll
    # from" rather than crediting a fake day-zero rollover.
    last_sample_at: float | None = None  # epoch seconds
    # nimbus issue #484: the PUBLISHED (guarded) commanded state -- this
    # is the load-relay-chatter guard's own persisted output, genuinely
    # separate from currently_on/on_since/off_since above (which track
    # what the load's REAL power sensor measured, not what the Solver
    # last told it to do). See decide_commanded_state()'s own docstring
    # for the hysteresis this exists to hold across 5-minute re-solves.
    commanded_state: bool = False
    commanded_since: float | None = None  # epoch seconds
    # nimbus issue #484: the in-progress CHALLENGE to commanded_state --
    # a raw_new_state that currently disagrees with commanded_state, and
    # the epoch it FIRST started disagreeing (consecutively -- reset the
    # moment the raw value stops matching this specific challenger). See
    # decide_commanded_state()'s own docstring for the full debounce
    # this pair implements.
    pending_state: bool | None = None
    pending_since: float | None = None  # epoch seconds
    # The output-layer gap #484's own docstring flagged ("no consumer
    # yet"): how many times TODAY dispatch_commanded_state() has actually
    # issued a real ON command for this load -- a genuinely new concept,
    # distinct from commanded_since (which tracks WHEN the current
    # published value took effect, not how many times it's flipped on).
    # Rolled to 0 on a day_key change via record_activation() itself (the
    # only writer of this field), same "reset at local midnight" posture
    # apply_power_sample() already uses for delivered_today_kwh -- kept as
    # its own small function rather than folded into apply_power_sample()
    # since activations are counted at DISPATCH time (a real service call
    # actually issued), not at every solve tick's power sample.
    activations_today: int = 0
    # nimbus issue #581 (Mark Purcell, real use the day after #578/#579
    # shipped): the LP already computes each load's own full per-period
    # plan every solve (AdequacyLoadPlan.power_kw / SheddableLoadPlan.
    # served_kw in solver/network.py) -- apply_commanded_state_guard() in
    # solver_writer.py used to read only period 0 of it, to decide the
    # current on/off state, and discard the rest every cycle. These seven
    # fields are that discarded series, persisted so NimbusControllable
    # LoadStateSensor can publish it. All default None -- a load kind this
    # doesn't apply to (plan_nominal_kw for a deferrable load, the four
    # plan_target_kwh/plan_shortfall_kwh/plan_earliest_period/plan_
    # deadline_period fields for a sheddable one), or a load not currently
    # in the plan at all this cycle (done, skipped, misconfigured), simply
    # never has these fields touched -- the sensor reads whatever was last
    # persisted, same "stale is fine, this bookkeeping is best-effort"
    # posture as commanded_state's own read path. `plan_forecast` is the
    # per-period series itself (power_kw for deferrable, served_kw for
    # sheddable) in the same {"time", "value"} shape every other Nimbus
    # forecast sensor already publishes (see build_time_value_series()
    # below); `plan_delivered_kwh_forecast` is the same shape but the
    # CUMULATIVE energy delivered through each period (deferrable only --
    # a sheddable load has no cumulative deadline target to track against).
    plan_forecast: list[dict[str, Any]] | None = None
    plan_delivered_kwh_forecast: list[dict[str, Any]] | None = None
    plan_target_kwh: float | None = None
    plan_shortfall_kwh: float | None = None
    plan_earliest_period: int | None = None
    plan_deadline_period: int | None = None
    plan_nominal_kw: float | None = None
    # nimbus issue #591 (Mark Purcell, part of #589 -- "how much will it
    # cost?" has a direct answer in the plan and no entity to carry it).
    # `plan_cost_forecast` is the per-period PLANNED cost series (power_kw
    # * period_hours * that period's own blended import_price, the same
    # price the solve itself read) in the same {"time", "value"} shape as
    # plan_forecast -- published every cycle alongside it, same posture.
    # `cost_today` is the ACTUAL cost accumulator: apply_power_sample()
    # folds in power_kw * dt_hours * the live import price at sample time,
    # the load-level version of the household's own hand-written
    # `hot_water_marginal_cost_daily` template (#534 inventory), now
    # computed by Nimbus from what it actually commanded. Resets to 0.0
    # on the same local-midnight day_key rollover as delivered_today_kwh
    # (see apply_power_sample()'s own day-rollover block).
    plan_cost_forecast: list[dict[str, Any]] | None = None
    cost_today: float = 0.0
    # nimbus issue #592 (Mark Purcell, part of #589 -- "will the tank be
    # at 60 by lunchtime?"): the two rates thermal_forecast.py's own
    # learn_thermal_rates() derives from recorder history, persisted so
    # they only need relearning once per calendar day (a real recorder
    # DB query, unlike the rest of a solve cycle) rather than every
    # solve -- see thermal_rates_learned_day_key. temperature_forecast
    # is the resulting projection (thermal_forecast.py's own
    # project_temperature_forecast()), refreshed every cycle from the
    # load's own already-current plan_forecast and live temperature,
    # same "cheap to redo, no reason to throttle it separately" posture
    # as every other per-cycle forecast field above. All None/""/0 for
    # a load with no done_entity configured (no temperature to model),
    # or one not (yet) of a kind this applies to.
    thermal_heating_rate_c_per_kwh: float | None = None
    thermal_idle_decay_c_per_hour: float | None = None
    thermal_rates_learned_day_key: str = ""
    temperature_forecast: list[dict[str, Any]] | None = None
    # nimbus issue #610 (Mark Purcell, real finding on the #534 SG Ready
    # bridge: current_temperature reads ~10-11 degC LOW while the
    # compressor is actively running -- a device-side reporting
    # artifact, confirmed by every idle reading before/after a run
    # agreeing with itself while every in-run reading is depressed).
    # The last real, trustworthy idle reading -- updated only when the
    # load is confirmed idle AND settled (see solver_writer.py's own
    # wiring for the settling-window logic) -- used as the temperature
    # projection's own starting point whenever the load is currently on
    # or has stopped too recently to trust a fresh live reading. None
    # for a load that has never yet had a trustworthy idle sample.
    last_idle_temperature: float | None = None
    # "learned" once at least one real idle-to-idle heating-rate sample
    # and one real idle-decay sample were found in recorder history;
    # "fallback" whenever either rate fell back to thermal_forecast.py's
    # own #592-cited defaults. "" for a load that has never yet run the
    # thermal-rate learner at all. nimbus issue #610: "the published
    # attributes do not say which [a learned rate from a default]."
    thermal_rates_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "currently_on": self.currently_on,
            "on_since": self.on_since,
            "off_since": self.off_since,
            "delivered_today_kwh": self.delivered_today_kwh,
            "carry_kwh": self.carry_kwh,
            "day_key": self.day_key,
            "last_sample_at": self.last_sample_at,
            "commanded_state": self.commanded_state,
            "commanded_since": self.commanded_since,
            "pending_state": self.pending_state,
            "pending_since": self.pending_since,
            "activations_today": self.activations_today,
            "plan_forecast": self.plan_forecast,
            "plan_delivered_kwh_forecast": self.plan_delivered_kwh_forecast,
            "plan_target_kwh": self.plan_target_kwh,
            "plan_shortfall_kwh": self.plan_shortfall_kwh,
            "plan_earliest_period": self.plan_earliest_period,
            "plan_deadline_period": self.plan_deadline_period,
            "plan_nominal_kw": self.plan_nominal_kw,
            "plan_cost_forecast": self.plan_cost_forecast,
            "cost_today": self.cost_today,
            "thermal_heating_rate_c_per_kwh": self.thermal_heating_rate_c_per_kwh,
            "thermal_idle_decay_c_per_hour": self.thermal_idle_decay_c_per_hour,
            "thermal_rates_learned_day_key": self.thermal_rates_learned_day_key,
            "temperature_forecast": self.temperature_forecast,
            "last_idle_temperature": self.last_idle_temperature,
            "thermal_rates_source": self.thermal_rates_source,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> LoadRunState:
        return LoadRunState(
            currently_on=bool(data.get("currently_on", False)),
            on_since=data.get("on_since"),
            off_since=data.get("off_since"),
            delivered_today_kwh=float(data.get("delivered_today_kwh", 0.0)),
            carry_kwh=float(data.get("carry_kwh", 0.0)),
            day_key=str(data.get("day_key", "")),
            last_sample_at=data.get("last_sample_at"),
            commanded_state=bool(data.get("commanded_state", False)),
            commanded_since=data.get("commanded_since"),
            pending_state=data.get("pending_state"),
            pending_since=data.get("pending_since"),
            activations_today=int(data.get("activations_today", 0)),
            plan_forecast=data.get("plan_forecast"),
            plan_delivered_kwh_forecast=data.get("plan_delivered_kwh_forecast"),
            plan_target_kwh=data.get("plan_target_kwh"),
            plan_shortfall_kwh=data.get("plan_shortfall_kwh"),
            plan_earliest_period=data.get("plan_earliest_period"),
            plan_deadline_period=data.get("plan_deadline_period"),
            plan_nominal_kw=data.get("plan_nominal_kw"),
            plan_cost_forecast=data.get("plan_cost_forecast"),
            cost_today=float(data.get("cost_today", 0.0)),
            thermal_heating_rate_c_per_kwh=data.get("thermal_heating_rate_c_per_kwh"),
            thermal_idle_decay_c_per_hour=data.get("thermal_idle_decay_c_per_hour"),
            thermal_rates_learned_day_key=str(
                data.get("thermal_rates_learned_day_key", "")
            ),
            temperature_forecast=data.get("temperature_forecast"),
            last_idle_temperature=data.get("last_idle_temperature"),
            thermal_rates_source=str(data.get("thermal_rates_source", "")),
        )


def build_time_value_series(
    grid_times: list[datetime], values: Any, *, round_ndigits: int = 3
) -> list[dict[str, Any]]:
    """nimbus issue #581: the standard {"time": ..., "value": ...} shape
    every other Nimbus forecast sensor already publishes (see coordinator.
    py's own per-signal forecast construction) -- one shared builder so a
    Controllable Load's own plan series is byte-shape-identical to every
    other forecast a household or dashboard already knows how to read,
    not a bespoke shape invented for this one sensor. `values` is any
    real-valued sequence the same length as `grid_times` (a numpy array
    or a plain list -- `float()` handles both)."""
    return [
        {"time": t.isoformat(), "value": round(float(v), round_ndigits)}
        for t, v in zip(grid_times, values)
    ]


def compute_rollover(
    *,
    quota_kwh_per_day: float,
    carry_prev_kwh: float,
    delivered_yesterday_kwh: float,
    carry_cap_kwh: float,
) -> float:
    """#479's own rollover formula, run once at each local-midnight
    day_key change: carry = min(max(0, quota + carry_prev - delivered),
    carry_cap). A day that fell short of quota rolls the shortfall forward
    as extra carry (up to the cap); a day that met or exceeded quota drains
    previously-owed carry instead of adding more, and never goes negative
    (a day that overshoots quota is not a debt against tomorrow)."""
    raw = quota_kwh_per_day + carry_prev_kwh - delivered_yesterday_kwh
    return min(max(0.0, raw), carry_cap_kwh)


def effective_target_kwh(
    *,
    quota_kwh_per_day: float,
    carry_kwh: float,
    max_per_day_kwh: float | None,
) -> float:
    """Today's real target the LP should see: quota plus whatever carried
    in, capped by max_per_day_kwh if the household set one (a day with
    max_per_day reached carries the remainder again rather than dropping
    it -- #479's own acceptance criteria -- since the cap only limits
    TODAY's target, never the carry_kwh value itself)."""
    target = quota_kwh_per_day + carry_kwh
    if max_per_day_kwh is not None:
        target = min(target, max_per_day_kwh)
    return target


def remaining_kwh(*, target_kwh: float, delivered_today_kwh: float) -> float:
    """The EMHASS "remaining hours" idea, computed by Nimbus instead of
    asked of the user -- what's left to deliver today, never negative."""
    return max(0.0, target_kwh - delivered_today_kwh)


def apply_power_sample(
    state: LoadRunState,
    *,
    now: datetime,
    day_key: str,
    power_kw: float,
    on_threshold_kw: float = DEFAULT_ON_THRESHOLD_KW,
    quota_kwh_per_day: float | None = None,
    carry_cap_kwh: float = 0.0,
    import_price_now: float | None = None,
) -> LoadRunState:
    """One solve-tick update. Rolls the day over first if `day_key` has
    changed since the last sample -- applying compute_rollover() only when
    quota_kwh_per_day is given (None for a non-quota-kind load, which still
    gets its own delivered_today_kwh/on-off tracking reset daily for #484's
    benefit, but never accrues carry, since #486 has no quota kind to
    configure one against yet) and only when state.day_key is non-empty
    (a load's very first-ever sample has no real "yesterday" to roll from)
    -- then folds in this sample's on/off transition and energy delta.

    nimbus issue #591: `import_price_now` is the live blended import price
    at THIS sample's own instant (the same figure the solve itself reads,
    solver_import_price_sensor) -- when given, this sample's own energy
    delta is also priced and folded into `cost_today`, the actual-cost
    counterpart to `delivered_today_kwh`, reset on the same rollover.
    `None` (a caller with no live price on hand, or every existing test/
    caller predating #591) is a genuine no-op: cost_today simply never
    accrues for that sample, exactly like an unconfigured optional field
    elsewhere in this project -- never a fabricated $0.00 claim."""
    now_ts = now.timestamp()
    if day_key != state.day_key:
        carry = (
            compute_rollover(
                quota_kwh_per_day=quota_kwh_per_day,
                carry_prev_kwh=state.carry_kwh,
                delivered_yesterday_kwh=state.delivered_today_kwh,
                carry_cap_kwh=carry_cap_kwh,
            )
            if quota_kwh_per_day is not None and state.day_key
            else 0.0
        )
        state = replace(
            state,
            delivered_today_kwh=0.0,
            carry_kwh=carry,
            day_key=day_key,
            cost_today=0.0,
        )

    is_on = power_kw > on_threshold_kw
    on_since = state.on_since
    off_since = state.off_since
    if is_on and not state.currently_on:
        on_since = now_ts
    elif not is_on and state.currently_on:
        off_since = now_ts

    delivered = state.delivered_today_kwh
    cost_today = state.cost_today
    if state.last_sample_at is not None:
        dt_hours = (now_ts - state.last_sample_at) / 3600.0
        if 0.0 < dt_hours <= MAX_SAMPLE_GAP_HOURS:
            delivered += power_kw * dt_hours
            if import_price_now is not None:
                cost_today += power_kw * dt_hours * import_price_now

    return replace(
        state,
        currently_on=is_on,
        on_since=on_since,
        off_since=off_since,
        delivered_today_kwh=delivered,
        cost_today=cost_today,
        last_sample_at=now_ts,
    )


def decide_commanded_state(
    state: LoadRunState,
    *,
    raw_new_state: bool,
    now: datetime,
    min_hysteresis_seconds: float,
) -> LoadRunState:
    """nimbus issue #484 (Mark's own cited HAEO incident: "a plan
    re-solved every few seconds drove 131 spurious relay states in a
    night"): the relay-chatter guard. `raw_new_state` is what THIS
    solve's own plan says a load should be running right now (its own
    period-0 scheduled power, above/below some on/off threshold) --
    genuinely free to flip every re-solve. What this function returns is
    the PUBLISHED `commanded_state`, which only adopts a disagreeing
    raw_new_state once it has held CONSECUTIVELY for at least
    `min_hysteresis_seconds` -- a debounce, not a rate limit: a raw value
    that flips back to agreeing with the current commanded_state even
    once resets the challenge entirely (this is what makes an
    indifferent load's period-0 decision flipping every single re-solve
    produce zero real published changes, not one every
    min_hysteresis_seconds -- see this module's own tests for the exact
    #484 acceptance scenario, "ten consecutive solves that flip an
    indifferent load's decision produce <= 1 commanded-state change").

    The very first-ever decision (commanded_since is None, i.e. this
    load has never been guarded before) always adopts raw_new_state
    immediately -- there is no prior commitment to protect yet, so an
    artificial startup delay would only be arbitrary, not a real guard
    against anything.

    Returns a full new LoadRunState (only commanded_state/commanded_since/
    pending_state/pending_since change; every other field is carried
    through unmodified) rather than a bare tuple, so a caller can pass
    its result straight to LoadRunStateStore.async_write() the same way
    apply_power_sample()'s own result already does."""
    now_ts = now.timestamp()

    if state.commanded_since is None:
        return replace(
            state,
            commanded_state=raw_new_state,
            commanded_since=now_ts,
            pending_state=None,
            pending_since=None,
        )

    if raw_new_state == state.commanded_state:
        # Not challenging the currently published value -- any
        # in-progress challenge for the OTHER value is now stale.
        if state.pending_state is not None:
            return replace(state, pending_state=None, pending_since=None)
        return state

    if state.pending_state != raw_new_state:
        # A new challenge (first time this specific value has disagreed
        # with commanded_state since the last time they last agreed) --
        # start its own clock, don't touch commanded_state yet.
        return replace(state, pending_state=raw_new_state, pending_since=now_ts)

    # The SAME challenger persisting since pending_since -- has it held
    # consecutively long enough to actually adopt?
    assert state.pending_since is not None  # pending_state matched above
    if now_ts - state.pending_since >= min_hysteresis_seconds:
        return replace(
            state,
            commanded_state=raw_new_state,
            commanded_since=now_ts,
            pending_state=None,
            pending_since=None,
        )
    return state


def activation_allowed(
    state: LoadRunState,
    *,
    max_activations_per_day: int | None,
    day_key: str,
) -> bool:
    """nimbus issue #534 item 3: the daily activation cap ("a cap of 3
    performance activations per 24h", the bridge's own real device-side
    constraint, made configurable per load rather than hard-coded).
    `max_activations_per_day=None` is the default/no-op -- every load with
    no cap configured is always allowed, matching every other optional
    field's convention in this project.

    Reads state.activations_today directly against `day_key` rather than
    calling record_activation() itself, so a caller can check BEFORE
    deciding whether to actually dispatch (dispatch_commanded_state()'s
    own real use) without side effects from the check alone. If
    state.day_key doesn't match day_key yet (the state hasn't rolled over
    for today), today's real count is 0 -- record_activation() below is
    what actually performs that roll, this function only reads."""
    if max_activations_per_day is None:
        return True
    today_count = state.activations_today if state.day_key == day_key else 0
    return today_count < max_activations_per_day


def record_activation(state: LoadRunState, *, day_key: str) -> LoadRunState:
    """Called by dispatch_commanded_state() exactly once per real ON
    command actually issued (never for a command that activation_allowed()
    blocked, and never for an OFF command -- the cap is specifically on
    "performance activations", per #534's own wording). Rolls
    activations_today to 0 first if `day_key` is a new day relative to
    state.day_key -- this is the field's only writer, so this is also the
    only place that roll happens; unlike apply_power_sample() above, this
    intentionally does NOT touch delivered_today_kwh/carry_kwh/currently_on
    -- those roll over on their own schedule from real power samples, this
    is a separate, dispatch-time count."""
    base = (
        state
        if state.day_key == day_key
        else replace(state, activations_today=0, day_key=day_key)
    )
    return replace(base, activations_today=base.activations_today + 1)


_SHORTFALL_EPSILON_KWH = 0.01
_TARGET_REACHED_EPSILON_KWH = 0.01
_SHED_EPSILON_KWH = 0.01

# Mirrors const.py's CONTROLLABLE_LOAD_KIND_DEFERRABLE/_SHEDDABLE string
# values without importing const.py -- this module stays free of any
# project-internal import, not just HA ones, so it keeps resolving
# identically under the standalone/cron deployment's own bare-module
# import shape (see this file's own top docstring).
_LOAD_KIND_DEFERRABLE = "deferrable"
_LOAD_KIND_SHEDDABLE = "sheddable"


@dataclass(frozen=True)
class ScheduleView:
    """nimbus issue #590 (Mark Purcell, real household ask: "I don't know
    if it is scheduled, what time and for how long. how much will it
    cost, what are the forecasts... "): the seven device-page answers
    derived from one load's own already-persisted LoadRunState, computed
    fresh on every read rather than persisted themselves -- unlike
    plan_forecast/plan_target_kwh/etc (#581), which genuinely need to
    survive a restart because they're written once per solve cycle and
    read many times between solves, this view is cheap pure arithmetic
    over fields already in memory, so recomputing it per sensor poll is
    simpler and can't drift from whatever the state store currently
    holds."""

    next_start: datetime | None
    next_end: datetime | None
    planned_duration_h: float | None
    planned_energy_kwh: float | None
    planned_cost: float | None
    delivered_today_kwh: float
    cost_today: float
    target_today_kwh: float | None
    status: str


def _find_current_or_next_run(
    forecast: list[dict[str, Any]], *, now: datetime, on_threshold_kw: float
) -> tuple[int, int] | None:
    """Scans `plan_forecast` (already time-ordered, one entry per solved
    period -- see build_time_value_series() above) for the first
    contiguous run of periods above `on_threshold_kw` that is either
    already in progress (its own period start <= now, and either it's
    the last published period or the next one hasn't started yet) or
    still upcoming (period start > now). Returns the run's own
    (start_idx, end_idx) inclusive, or None if nothing in the published
    horizon ever exceeds the threshold (a load that's fully idle for the
    whole forecast -- done, capped, or genuinely has nothing scheduled)."""
    n = len(forecast)
    if n == 0:
        return None
    times = [datetime.fromisoformat(e["time"]) for e in forecast]
    values = [float(e["value"]) for e in forecast]
    start_idx = None
    for i in range(n):
        if values[i] <= on_threshold_kw:
            continue
        period_start = times[i]
        period_end = times[i + 1] if i + 1 < n else None
        active_now = period_start <= now and (period_end is None or now < period_end)
        if active_now or period_start > now:
            start_idx = i
            break
    if start_idx is None:
        return None
    end_idx = start_idx
    while end_idx + 1 < n and values[end_idx + 1] > on_threshold_kw:
        end_idx += 1
    return start_idx, end_idx


def _period_duration_hours(times: list[datetime], i: int) -> float:
    """This tiered grid's own periods aren't a uniform length -- the real
    duration of period i is the gap to period i+1. The very last
    published period has no "next" to measure against; falls back to the
    PRECEDING gap (or 30 minutes if there's only one period total) as the
    best available estimate rather than treating it as zero-length, which
    would silently truncate a run's own final period out of its planned
    duration/energy."""
    if i + 1 < len(times):
        return (times[i + 1] - times[i]).total_seconds() / 3600.0
    if i > 0:
        return (times[i] - times[i - 1]).total_seconds() / 3600.0
    return 0.5


def derive_schedule_view(
    state: LoadRunState,
    *,
    load_kind: str,
    now: datetime,
    on_threshold_kw: float = DEFAULT_ON_THRESHOLD_KW,
    max_activations_per_day: int | None = None,
    tank_current_temperature: float | None = None,
) -> ScheduleView:
    """nimbus issue #590: the seven-entity device-page view, computed
    entirely from what #479/#484/#581 already persist -- no new solver
    plumbing needed. `load_kind` selects which of the deferrable-only
    (plan_target_kwh/plan_shortfall_kwh) vs sheddable-only (plan_nominal_kw)
    fields are meaningful; the other kind's fields are simply None on the
    state already (#581's own convention), so this function never has to
    special-case "field wasn't populated this cycle" beyond that.

    `tank_current_temperature` is deliberately the CALLER's job to have
    already read (via the exact same _evaluate_done_condition()-style
    current_temperature attribute read solver_writer.py uses for #534's
    own done condition, see that function's own docstring) -- this
    function never touches hass.states itself, keeping it HA-import-free
    like the rest of this module.
    """
    forecast = state.plan_forecast or []
    run = _find_current_or_next_run(forecast, now=now, on_threshold_kw=on_threshold_kw)

    next_start: datetime | None = None
    next_end: datetime | None = None
    planned_duration_h: float | None = None
    planned_energy_kwh: float | None = None
    planned_cost: float | None = None

    if run is not None:
        start_idx, end_idx = run
        times = [datetime.fromisoformat(e["time"]) for e in forecast]
        values = [float(e["value"]) for e in forecast]
        next_start = times[start_idx]
        next_end = times[end_idx + 1] if end_idx + 1 < len(times) else None
        planned_duration_h = sum(
            _period_duration_hours(times, i) for i in range(start_idx, end_idx + 1)
        )
        if load_kind == _LOAD_KIND_DEFERRABLE and state.plan_delivered_kwh_forecast:
            delivered_series = [
                float(e["value"]) for e in state.plan_delivered_kwh_forecast
            ]
            if end_idx < len(delivered_series):
                before = delivered_series[start_idx - 1] if start_idx > 0 else 0.0
                planned_energy_kwh = round(delivered_series[end_idx] - before, 3)
        if planned_energy_kwh is None:
            # Sheddable (no cumulative deadline series to lean on), or a
            # deferrable load whose delivered-kwh series wasn't published
            # this cycle (stale/partial state) -- fall back to integrating
            # the plan's own power values directly over the run.
            planned_energy_kwh = round(
                sum(
                    values[i] * _period_duration_hours(times, i)
                    for i in range(start_idx, end_idx + 1)
                ),
                3,
            )
        # nimbus issue #591: plan_cost_forecast is a per-period series
        # (power_kw * period_hours * that period's own blended
        # import_price -- see solver_writer.py's own construction),
        # aligned one-to-one with plan_forecast, so simply summing it
        # over the same [start_idx, end_idx] run window answers "how
        # much will THIS run cost" without any further price lookup
        # here. None (not 0.0) whenever the series wasn't published this
        # cycle -- an unpriced run is an honest "unknown", never a
        # fabricated free run.
        if state.plan_cost_forecast and end_idx < len(state.plan_cost_forecast):
            cost_series = [float(e["value"]) for e in state.plan_cost_forecast]
            planned_cost = round(sum(cost_series[start_idx : end_idx + 1]), 3)

    target_today_kwh = (
        state.plan_target_kwh if load_kind == _LOAD_KIND_DEFERRABLE else None
    )

    day_key = now.strftime("%Y-%m-%d")
    activations_today = state.activations_today if state.day_key == day_key else 0

    status: str
    if state.commanded_state:
        status = "running"
    elif (
        load_kind == _LOAD_KIND_DEFERRABLE
        and state.plan_shortfall_kwh is not None
        and state.plan_shortfall_kwh > _SHORTFALL_EPSILON_KWH
    ):
        status = f"will miss target by {state.plan_shortfall_kwh:.2f} kWh"
    elif (
        max_activations_per_day is not None
        and activations_today >= max_activations_per_day
    ):
        status = f"capped ({activations_today}/{max_activations_per_day})"
    elif (
        target_today_kwh is not None
        and target_today_kwh > 0.0
        and state.delivered_today_kwh >= target_today_kwh - _TARGET_REACHED_EPSILON_KWH
    ):
        status = (
            f"done (tank {tank_current_temperature:.0f} °C)"
            if tank_current_temperature is not None
            else "done"
        )
    elif load_kind == _LOAD_KIND_SHEDDABLE and forecast and state.plan_nominal_kw:
        times_all = [datetime.fromisoformat(e["time"]) for e in forecast]
        values_all = [float(e["value"]) for e in forecast]
        shed_kwh = sum(
            max(0.0, state.plan_nominal_kw - values_all[i])
            * _period_duration_hours(times_all, i)
            for i in range(len(forecast))
            if times_all[i] <= now
        )
        if shed_kwh > _SHED_EPSILON_KWH:
            status = f"shed {shed_kwh:.2f} kWh today"
        elif run is not None:
            status = (
                f"scheduled {next_start:%H:%M}–{next_end:%H:%M}"
                if next_end
                else f"scheduled from {next_start:%H:%M}"
            )
        else:
            status = "outside window"
    elif run is not None:
        status = (
            f"scheduled {next_start:%H:%M}–{next_end:%H:%M}"
            if next_end is not None
            else f"scheduled from {next_start:%H:%M}"
        )
    else:
        status = "outside window"

    return ScheduleView(
        next_start=next_start,
        next_end=next_end,
        planned_duration_h=(
            round(planned_duration_h, 3) if planned_duration_h is not None else None
        ),
        planned_energy_kwh=planned_energy_kwh,
        planned_cost=planned_cost,
        delivered_today_kwh=state.delivered_today_kwh,
        cost_today=round(state.cost_today, 3),
        target_today_kwh=target_today_kwh,
        status=status,
    )


@dataclass
class LoadRunStateStore:
    """One Store per hub -- every configured Controllable Load's own
    run-state lives in the SAME small JSON file (keyed by subentry_id),
    same reasoning as number.py's own _SharedNumberStore: independent
    per-load Store instances editing the same file would race a
    read-modify-write against each other if two loads are sampled back
    to back. `store` is a real homeassistant.helpers.storage.Store
    instance, passed in by the caller (native mode only -- see this
    module's own top docstring) rather than constructed here, so this
    file never has to import homeassistant.* at module level."""

    store: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def async_read(self, load_key: str) -> LoadRunState:
        try:
            data = await self.store.async_load()
        except Exception:  # noqa: BLE001 -- a corrupt/unreadable store file
            # must never block a solve cycle; this is a durability
            # BACKSTOP (same posture as _SharedNumberStore's own reads),
            # not a required dependency -- a fresh LoadRunState is a
            # perfectly safe "never sampled" starting point.
            return LoadRunState()
        if not data or load_key not in data:
            return LoadRunState()
        try:
            return LoadRunState.from_dict(data[load_key])
        except (TypeError, ValueError):
            return LoadRunState()

    async def async_write(self, load_key: str, state: LoadRunState) -> None:
        async with self.lock:
            try:
                data = await self.store.async_load() or {}
            except Exception:  # noqa: BLE001 -- same reasoning as async_read
                data = {}
            data[load_key] = state.to_dict()
            await self.store.async_save(data)
