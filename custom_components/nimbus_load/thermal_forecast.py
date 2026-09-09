"""nimbus issue #592 (Mark Purcell, part of #589 -- "will the tank be at
60 by lunchtime?"): groundwork for the full #481 thermal kind, scoped to
exactly what a household can already answer from a Controllable Load's
own existing config -- no new wizard field, no tank volume to type.

Two pure functions, deliberately HA-import-free (same posture as
done_condition.py, for the same reason -- this needs to be callable from
a plain sensor poll without paying solver_writer.py's own numpy/highspy
import cost):

- `learn_thermal_rates()`: from a load's own recent (timestamp,
  temperature, power_kw) recorder history, learns a heating rate
  (deg C gained per kWh delivered while genuinely heating) and an idle
  decay rate (deg C lost per hour while off), each as a plain average
  across the real segments found. Falls back to nimbus issue #592's own
  cited real-household figures (6/7 Sep on the #534 install) when there
  isn't enough history yet to learn from -- a fresh install answers
  "roughly" from day one rather than "unknown."
- `project_temperature_forecast()`: walks an already-published plan
  (the same plan_forecast every other Controllable Load forecast sensor
  already publishes, #581) forward from a live starting temperature,
  applying the learned heating/decay rates period by period.

Naming follows EMHASS's own `thermal_config` vocabulary (heating_rate,
cooling_constant) per nimbus issue #603's own standing rule -- adopt an
adjacent open-source project's already-solved semantics rather than
inventing new ones for the same real physics.

Deliberately NOT part of this module (left open, see nimbus issue #592's
own worklog entry for the full reasoning): marking the forecast as
"model-based" when the temperature source is a power-based estimate
rather than a real thermistor -- there is no reliable, non-hardcoded way
to tell the two apart from a done_entity's own config alone, and #592
never asks for a new wizard field to disambiguate. Also not part of this
module: using the projected crossing of the done temperature to shorten
the LP's own schedule ahead of time (#592's own closing paragraph frames
this as a future consequence of having the forecast, not a requirement
of publishing it).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

# nimbus issue #592's own cited real-household figures (6/7 Sep 2026,
# the #534 heat pump: 48->61 degC in 2.9h and 1.6kWh; idle 54.5->53.8
# degC overnight) -- used whenever recorder history doesn't yet have
# enough real segments to learn from.
DEFAULT_HEATING_RATE_C_PER_KWH = 8.0
DEFAULT_IDLE_DECAY_C_PER_HOUR = 0.5

# A run that never draws above this fraction of its own on_threshold_kw
# never really started heating (a compressor start-up blip, a bridge
# reporting "on" a few seconds before real draw begins) -- nimbus issue
# #592's own caveat: "when the pump does not reach its heating threshold
# the rate should not be learned from that run."
_MIN_HEATING_SEGMENT_HOURS = 0.1  # 6 minutes -- below this, too short to trust

# nimbus issue #610 (Mark Purcell, real finding on the #534 SG Ready
# bridge: current_temperature reads ~10-11 degC LOW while the compressor
# is actively running -- a device-side reporting artifact of this
# specific bridge, not a real physical temperature, confirmed by every
# idle reading immediately before/after a run agreeing with itself while
# every in-run reading is depressed). Reading temperature DURING a
# heating segment is therefore never trustworthy on this class of
# device -- the heating rate must be learned from the two IDLE readings
# bracketing a run (last idle reading before it started, first SETTLED
# idle reading after it stopped), never from the heating segment's own
# start/end samples. Settling takes about one 5-minute recorder bucket
# on Mark's own unit (both his real runs read the true value within one
# 5-minute bucket of stopping) -- used as the generic default here since
# no per-device settling time is configured anywhere in this project.
SETTLING_MINUTES = 5.0


class LearnedThermalRates(NamedTuple):
    heating_rate_c_per_kwh: float
    idle_decay_c_per_hour: float
    is_learned: bool  # False when either rate fell back to the default
    # nimbus issue #610: "the published attributes do not say which [a
    # learned rate from a default]" -- real sample counts so a reader
    # (or a future confidence-weighted learner) can tell "learned from
    # one barely-qualifying run" from "learned from a week of real
    # data," not just learned-vs-not.
    heating_sample_count: int
    idle_sample_count: int


def learn_thermal_rates(
    history: list[tuple[datetime, float, float]],
    *,
    on_threshold_kw: float,
) -> LearnedThermalRates:
    """`history` is (timestamp, temperature_c, power_kw) samples, already
    time-ordered, covering the load's own recent recorder window (the
    caller's job -- this function does no fetching of its own, same
    HA-import-free posture as done_condition.py). Splits it into
    contiguous heating segments (power > on_threshold_kw throughout) and
    idle segments (power <= on_threshold_kw throughout).

    Idle decay is computed directly from each idle segment's own start/
    end readings (idle readings are the trustworthy ones -- see #610's
    own module-level comment above). Heating rate is computed from the
    IDLE-TO-IDLE pair bracketing each qualifying heating segment (the
    last reading of the idle segment immediately before it, and the
    first SETTLED reading -- at least SETTLING_MINUTES after the
    heating segment ends -- of the idle segment immediately after it),
    never from the heating segment's own readings directly. A heating
    segment with no real idle segment on both sides (the very first or
    last segment in the window), or whose following idle segment never
    reaches a settled sample within the fetched history, contributes no
    heating-rate sample -- an honest "not enough data around this run
    yet" rather than a rate computed from readings #610 confirms are
    wrong for this class of device.

    A heating segment shorter than _MIN_HEATING_SEGMENT_HOURS is dropped
    (#592's own "did not reach its heating threshold" caveat) rather
    than learning a rate from a run that barely started.

    Returns the #592-cited real-household defaults, with is_learned=
    False, whenever fewer than one qualifying segment of either kind was
    found -- a fresh install (or one still building up recorder history)
    answers "roughly" from day one, never "unknown," but the caller can
    still tell the two cases apart via is_learned if it wants to."""
    segments: list[tuple[int, int, bool]] = []
    if len(history) >= 2:
        seg_start_idx = 0
        seg_is_heating = history[0][2] > on_threshold_kw
        for i in range(1, len(history) + 1):
            still_same = i < len(history) and (
                (history[i][2] > on_threshold_kw) == seg_is_heating
            )
            if still_same:
                continue
            segments.append((seg_start_idx, i - 1, seg_is_heating))
            if i < len(history):
                seg_start_idx = i
                seg_is_heating = history[i][2] > on_threshold_kw

    decay_rates: list[float] = []
    for seg_start, seg_end, is_heating in segments:
        if is_heating or seg_end <= seg_start:
            continue
        t0, temp0, _ = history[seg_start]
        t1, temp1, _ = history[seg_end]
        hours = (t1 - t0).total_seconds() / 3600.0
        if hours > 0 and temp1 < temp0:
            decay_rates.append((temp0 - temp1) / hours)

    heating_rates: list[float] = []
    for idx, (seg_start, seg_end, is_heating) in enumerate(segments):
        if not is_heating:
            continue
        hours = (history[seg_end][0] - history[seg_start][0]).total_seconds() / 3600.0
        if hours < _MIN_HEATING_SEGMENT_HOURS:
            continue
        if idx == 0 or idx == len(segments) - 1:
            continue  # no real idle segment on one side within this history
        _before_start, before_end, before_is_heating = segments[idx - 1]
        after_start, after_end, after_is_heating = segments[idx + 1]
        if before_is_heating or after_is_heating:
            continue  # defensive -- segments always alternate by construction
        idle_before_temp = history[before_end][1]
        settle_deadline = history[seg_end][0] + timedelta(minutes=SETTLING_MINUTES)
        settled_idx = next(
            (
                j
                for j in range(after_start, after_end + 1)
                if history[j][0] >= settle_deadline
            ),
            None,
        )
        if settled_idx is None:
            continue  # this run's own idle-after segment never settled in view
        gain = history[settled_idx][1] - idle_before_temp
        if gain <= 0:
            continue
        kwh = sum(
            history[j][2] * (history[j + 1][0] - history[j][0]).total_seconds() / 3600.0
            for j in range(seg_start, seg_end)
        )
        if kwh > 1e-6:
            heating_rates.append(gain / kwh)

    heating_rate = (
        sum(heating_rates) / len(heating_rates)
        if heating_rates
        else DEFAULT_HEATING_RATE_C_PER_KWH
    )
    decay_rate = (
        sum(decay_rates) / len(decay_rates)
        if decay_rates
        else DEFAULT_IDLE_DECAY_C_PER_HOUR
    )
    return LearnedThermalRates(
        heating_rate_c_per_kwh=round(heating_rate, 4),
        idle_decay_c_per_hour=round(decay_rate, 4),
        is_learned=bool(heating_rates) and bool(decay_rates),
        heating_sample_count=len(heating_rates),
        idle_sample_count=len(decay_rates),
    )


def project_temperature_forecast(
    plan_forecast: list[dict[str, object]],
    *,
    start_temperature: float,
    heating_rate_c_per_kwh: float,
    idle_decay_c_per_hour: float,
    on_threshold_kw: float,
    ceiling_temperature: float | None = None,
    override_first_period_power_kw: float | None = None,
) -> list[dict[str, object]]:
    """Walks `plan_forecast` (the standard {"time","value"} kW series
    #581 already publishes on every Controllable Load) forward from
    `start_temperature`, period by period: a period whose own planned
    power exceeds `on_threshold_kw` gains `heating_rate_c_per_kwh` times
    that period's own real delivered kWh (power * the real gap to the
    NEXT period -- the same tiered-grid-aware duration every other
    per-period integration in this project uses, never a flat constant);
    any other period loses `idle_decay_c_per_hour` times that same real
    duration. The final published period has no "next" to measure a
    duration against -- reuses the preceding gap (or 0.5h if there is
    only one period total), same fallback load_run_state.py's own
    _period_duration_hours() already uses for exactly this edge.

    `ceiling_temperature` (nimbus issue #610, Mark Purcell's own
    caveat: "no ceiling: the projection keeps adding heating_rate x kWh
    past the heater's own setpoint"): when given, the projected value is
    clamped to never EXCEED it after a heating gain (never applied to a
    decay step -- decay is real and should be free to fall below any
    ceiling). None (the caller's job when there's no real setpoint to
    read) is a complete no-op, same posture as every other optional
    field in this project.

    `override_first_period_power_kw` (nimbus issue #611, Mark Purcell:
    "the forecast is projected from plan_forecast, so it shows [cooling]
    over [25 minutes] while [real power] is actually going into the
    tank" -- during the guard's own hold window, #595's own already-
    dispatched commanded_state can genuinely disagree with THIS cycle's
    freshly-solved plan_forecast[0]): when given, replaces plan_forecast's
    own period-0 power for this projection only (every later period
    still projects from the real plan) -- the caller's job to decide
    what that real, currently-commanded power actually is. None is a
    complete no-op, same as every pre-#611 caller.

    Returns the projected {"time","value"} temperature series, one
    point per plan_forecast period -- deliberately does NOT stop
    projecting once the temperature crosses any particular target; that
    interpretation (a done line, a deadline) is the caller's/consumer's
    job to draw on top of this honest physical projection."""
    if not plan_forecast:
        return []
    times = [
        e["time"] if isinstance(e["time"], datetime) else _parse_iso(e["time"])
        for e in plan_forecast
    ]
    powers = [float(e["value"]) for e in plan_forecast]
    if override_first_period_power_kw is not None and powers:
        powers[0] = override_first_period_power_kw
    n = len(plan_forecast)

    out: list[dict[str, object]] = []
    temp = start_temperature
    for i in range(n):
        if i + 1 < n:
            hours = (times[i + 1] - times[i]).total_seconds() / 3600.0
        elif i > 0:
            hours = (times[i] - times[i - 1]).total_seconds() / 3600.0
        else:
            hours = 0.5
        if powers[i] > on_threshold_kw:
            temp += powers[i] * hours * heating_rate_c_per_kwh
            if ceiling_temperature is not None:
                temp = min(temp, ceiling_temperature)
        else:
            temp -= idle_decay_c_per_hour * hours
        out.append({"time": plan_forecast[i]["time"], "value": round(temp, 2)})
    return out


def _parse_iso(s: object) -> datetime:
    return datetime.fromisoformat(str(s))
