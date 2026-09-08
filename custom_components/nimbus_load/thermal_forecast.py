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

from datetime import datetime
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


class LearnedThermalRates(NamedTuple):
    heating_rate_c_per_kwh: float
    idle_decay_c_per_hour: float
    is_learned: bool  # False when either rate fell back to the default


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
    idle segments (power <= on_threshold_kw throughout), computes each
    segment's own real degC-per-kWh (heating) or degC-per-hour (idle),
    and averages across whichever segments qualify.

    A heating segment shorter than _MIN_HEATING_SEGMENT_HOURS is dropped
    (#592's own "did not reach its heating threshold" caveat) rather
    than learning a rate from a run that barely started. An idle segment
    has no minimum -- even a short gap between two heating runs is real
    decay data, and decay is a slow, low-noise process compared to a
    compressor's own start-up transient.

    Returns the #592-cited real-household defaults, with is_learned=
    False, whenever fewer than one qualifying segment of either kind was
    found -- a fresh install (or one still building up recorder history)
    answers "roughly" from day one, never "unknown," but the caller can
    still tell the two cases apart via is_learned if it wants to."""
    heating_rates: list[float] = []
    decay_rates: list[float] = []

    if len(history) >= 2:
        seg_start_idx = 0
        seg_is_heating = history[0][2] > on_threshold_kw
        for i in range(1, len(history) + 1):
            still_same = i < len(history) and (
                (history[i][2] > on_threshold_kw) == seg_is_heating
            )
            if still_same:
                continue
            seg_end_idx = i - 1
            if seg_end_idx > seg_start_idx:
                t0, temp0, _ = history[seg_start_idx]
                t1, temp1, _ = history[seg_end_idx]
                hours = (t1 - t0).total_seconds() / 3600.0
                if hours > 0:
                    delta_c = temp1 - temp0
                    if seg_is_heating:
                        if hours >= _MIN_HEATING_SEGMENT_HOURS and delta_c > 0:
                            # kWh actually delivered across this segment:
                            # each sample's own power held constant until
                            # the NEXT sample (a plain trapezoid-free
                            # step integral -- consistent with how every
                            # other per-period integration in this
                            # project reads a power series).
                            kwh = sum(
                                history[j][2]
                                * (history[j + 1][0] - history[j][0]).total_seconds()
                                / 3600.0
                                for j in range(seg_start_idx, seg_end_idx)
                            )
                            if kwh > 1e-6:
                                heating_rates.append(delta_c / kwh)
                    elif delta_c < 0:
                        decay_rates.append(-delta_c / hours)
            if i < len(history):
                seg_start_idx = i
                seg_is_heating = history[i][2] > on_threshold_kw

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
    )


def project_temperature_forecast(
    plan_forecast: list[dict[str, object]],
    *,
    start_temperature: float,
    heating_rate_c_per_kwh: float,
    idle_decay_c_per_hour: float,
    on_threshold_kw: float,
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
        else:
            temp -= idle_decay_c_per_hour * hours
        out.append({"time": plan_forecast[i]["time"], "value": round(temp, 2)})
    return out


def _parse_iso(s: object) -> datetime:
    return datetime.fromisoformat(str(s))
