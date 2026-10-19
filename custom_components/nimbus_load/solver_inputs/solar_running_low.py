"""Solar "running low" gate (nimbus issue #1784, Mark Purcell).

On overcast days every day-ahead PV forecast the reference households track
runs far too high, Solcast's p50 included: on 15 Oct 2026 actual PV was
38.3 kWh against a p50 of 83.2 kWh (+117%). Overcast days land at roughly
45-70% of p50, close to Solcast's own p10. A plan built on p50 holds the
battery for solar that never arrives.

## The gate (Mark's option (a), as specified on the issue)

- Measure actual PV as a 30-minute mean of `solver_solar_power_sensor`.
- Compare it with a REFERENCE: the lower of the p50 forecast for the current
  30-minute period and for the period 30 minutes earlier. A 30-minute mean
  trails a rising morning curve by about its own length, so against the
  current p50 alone it trips every dawn (0.07 kW against 1.10 kW at 06:20 on
  his install). The lagged value fixes the morning; taking the LOWER of the
  two also stops a falling afternoon curve, where the mean lags the other
  way, reading as low output.
- Judge only while both p50s are at least a minimum (2 kW by default). The
  first hour of production runs low and noisy against the forecast.
- Hysteresis: on below 70% of the reference, off above 90%, so passing
  clouds do not make it flap.
- Confirmation (ours, not in the ha-config template): it turns on only once
  every judged reading for 10 minutes has stayed below 70%. Measured on the
  reference household on 16 Oct 2026: a late start tripped the bare gate at
  07:30 (ratio 0.61) and released it at 07:47, with actual above Solcast's
  p90 by 08:07. The ratio crossed 0.70 at 07:38, so a 10-minute
  confirmation never trips. On a really dull day it costs at most 10
  minutes on a whole-day correction. One morning of evidence: neither
  forecast's history is recorded, so the trip rate further back cannot be
  measured. 0 = no confirmation (on at the first low reading). Release
  stays immediate.

Whenever the gate cannot judge (switched off, no PV sensor, no reference,
below the minimum, no history, history running behind) it is OFF -- exactly the ha-config template,
which returns false on every one of those. So the gate also switches itself
off each evening once p50 falls under the minimum, and "today only" needs no
date bookkeeping.

## Which forecast is the reference

Mark's review on #1787 left the implementation to us, and the reference
household's solve is NOT driven by Solcast alone: its source 1 is Nimbus's
own learned forecast, with Solcast and Open-Meteo auto-included. So:

1. **Solcast, whenever Solcast's `detailedForecast` exists on the install**
   (in the blend or not). It is an independent, period-structured forecast
   whose earlier half-hours are still in its attributes, so the lagged p50
   is a read, not a memory. This is Mark's own reference.
2. **Otherwise the blend itself**: the blended p50 for "now" is recorded in
   process each solve, and the lagged value is the record from 30-40
   minutes ago. Without one (the first 30 minutes after a restart) the gate
   cannot judge and is off -- never a guess.

## What changes while it is on

The remaining periods of TODAY (index >= 1, local date) of each blend
member, BEFORE blending, the band, the index-0 live anchor, #1259 and #128:

- **Solcast with p10**: its own p10 (`pv_estimate10`). Mark's path.
- **A member carrying a genuine lower bound** (a lower quantile below its
  value -- Nimbus's own forecaster publishes an 80% band, so its lower is a
  nominal p10): that lower bound.
- **A member with no lower bound** (Open-Meteo's band is zero-width, and a
  Solcast forecast without p10, whose missing p10 the shared reshape fills
  with 0.0): option (b), its p50 scaled by the measured delivery ratio
  (the gate's own 30-minute actual / reference), clamped to
  [`RATIO_FLOOR`, 1.0] x p50. The floor is 0.3 because overcast days land
  at 45-70% of p50 (the issue's table; the worst, 15 Oct, 0.46), so it does
  not bind on a real dull day, while it stops one dark half-hour at ratio
  ~0.1 from wiping solar out of the whole rest of the day.

Later days are untouched: same-day evidence says nothing about tomorrow.

## Why recorder history and not an in-process EMA

ha-config's average is a template EMA updated every minute. That is the
right tool in HA YAML, where a new integration needs a restart. Here the
mean is computed from the recorder's own history of the PV sensor on every
solve instead, for one reason: it is correct straight after a restart. An
EMA held in this process would start from nothing and read low for its
first half hour -- exactly the "running low" signature -- after every HA
restart or Nimbus reload. If that history cannot be read, the gate is off.

The history can also be readable but BEHIND: while Home Assistant takes a
backup it locks the recorder's database, and new states are queued in memory
until the backup ends. The read then returns the last state written before
the lock, held to "now", and the 30-minute mean freezes on it. Measured on the
reference household on 20 Oct 2026: a manual backup locked the recorder at
07:17, when PV was 1.29 kW; at 08:28 the gate still read a 1.297 kW mean
against a live 7.8 kW, judged ratio 0.29 and planned the rest of the day on
30 % of Solcast while the battery sat at its reserve and the surplus was
exported. So the newest recorded change is compared with the live sensor's
own last change: when the recorder is more than `RECORDER_BEHIND_AFTER`
behind, the gate cannot judge and is off ("recorder_behind"). A sensor that
simply held one value is not behind -- its live last change is the recorded
one.

The gate's ON/OFF state is held in this process between solves, as the
issue allows. After a restart it starts off and re-judges at the next solve.

Prior art (per this repo's CHECK PRIOR ART directive): HAEO itself has
none -- this is purcell-lab/ha-config's template around HAEO, ported. EMHASS:
`pv_bias_calibration.py` tunes a P10/P50 blend weight once per resolved DAY,
with no same-day switch; neither has an intraday running-low gate.

Never raises: every failure path is a "not judged" result with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


# The HA-bridge helpers (ha_get, entity_exists, fetch_entity_history_range,
# resample_history_mean, _kw_scale_factor, _cfg_num, parse_iso) are reached
# through `sw`, the `solver_writer` MODULE, which the caller passes in --
# solar.py already holds it through its own deferred seam (see this
# package's `__init__.py`). Passing it rather than importing it keeps the
# test suite's `patch.object(solver_writer, ...)` seams working and adds no
# new solver_inputs -> solver_writer exception to the import-linter layer
# map.

# Settings (number entities). Mark's own defaults on the issue.
CFG_MIN_P50_KW = "solver_solar_running_low_min_p50_kw"
CFG_ON_PERCENT = "solver_solar_running_low_on_percent"
CFG_OFF_PERCENT = "solver_solar_running_low_off_percent"
CFG_CONFIRM_MINUTES = "solver_solar_running_low_confirm_minutes"
# 0 = off, the repo's convention for a feature carried by a number.
DEFAULT_MIN_P50_KW = 2.0
DEFAULT_ON_PERCENT = 70.0
DEFAULT_OFF_PERCENT = 90.0
DEFAULT_CONFIRM_MINUTES = 10.0

# Solcast's detailedForecast is 30-minute periods; the average spans the
# same length, which is why the lagged reference is one period back.
SOLCAST_PERIOD = timedelta(minutes=30)
ACTUAL_WINDOW = timedelta(minutes=30)
REFERENCE_LAG = timedelta(minutes=30)
# The blend-reference memory: the record used for "30 minutes earlier" must
# sit 30-40 minutes back; older than that is not the lagged period.
BLEND_LAG_TOLERANCE = timedelta(minutes=10)
BLEND_MEMORY_SPAN = timedelta(hours=2)
# How far the recorder's newest change may trail the live sensor's last
# change before the history is "behind" (a backup's database lock) -- see the
# module docstring. A live state reaches the recorder within seconds, so five
# minutes is never an ordinary commit delay.
RECORDER_BEHIND_AFTER = timedelta(minutes=5)
# Option (b)'s floor, as a share of p50 -- see the module docstring.
RATIO_FLOOR = 0.3

REFERENCE_SOLCAST = "solcast"
REFERENCE_BLEND = "blend"

METHOD_P10 = "p10"
METHOD_LOWER_BOUND = "lower_bound"
METHOD_RATIO = "ratio_scaled"


@dataclass
class _GateState:
    active: bool = False
    on_since: datetime | None = None
    # First judged low reading of the current confirmation window, or None.
    pending_since: datetime | None = None
    # (time, blended p50 kW at that time) for the blend reference.
    blend_memory: list[tuple[datetime, float]] = field(default_factory=list)


# In-process, by design (see the module docstring). Starts off.
_STATE = _GateState()
# Log-once set for the "inert on this install" DEBUG lines.
_INERT_LOGGED: set[str] = set()


def reset_gate_state() -> None:
    """Back to a fresh process: gate off, no memory, log-once set empty."""
    _STATE.active = False
    _STATE.on_since = None
    _STATE.pending_since = None
    _STATE.blend_memory.clear()
    _INERT_LOGGED.clear()


def _gate_result(
    reason: str,
    *,
    reference_source: str | None = None,
    actual_kw: float | None = None,
    p50_now_kw: float | None = None,
    p50_lagged_kw: float | None = None,
) -> dict[str, Any]:
    """The published evidence, one fixed shape whatever happened, so a
    consumer never sees a key appear and vanish between cycles (#589)."""
    reference = (
        min(p50_now_kw, p50_lagged_kw)
        if p50_now_kw is not None and p50_lagged_kw is not None
        else None
    )
    ratio = (
        actual_kw / reference
        if actual_kw is not None and reference is not None and reference > 0
        else None
    )

    def r(v: float | None) -> float | None:
        return round(v, 3) if v is not None else None

    return {
        "active": _STATE.active,
        "reason": reason,
        "reference_source": reference_source,
        "actual_30min_avg_kw": r(actual_kw),
        "p50_now_kw": r(p50_now_kw),
        "p50_lagged_kw": r(p50_lagged_kw),
        "reference_kw": r(reference),
        "ratio": round(ratio, 4) if ratio is not None else None,
        "on_since": _STATE.on_since.isoformat() if _STATE.on_since else None,
        "pending_since": (
            _STATE.pending_since.isoformat() if _STATE.pending_since else None
        ),
        # Filled in by build_solar_arrays() once it knows what it changed;
        # 0 / None while off.
        "periods_lowered": 0,
        "members_p10": 0,
        "members_lower_bound": 0,
        "members_ratio_scaled": 0,
        "ratio_scale_used": None,
    }


def _idle_result() -> dict[str, Any]:
    return _gate_result("not_evaluated")


# The latest evaluation, read by the plan publish (solve_diagnostics). Set
# by every build_solar_arrays() call (solar._apply_running_low_gate).
LAST_RESULT: dict[str, Any] = _idle_result()


def record_result(result: dict[str, Any]) -> None:
    """Make `result` the evaluation the next publish reads. Mutates in
    place, so a module that imported LAST_RESULT sees the new value."""
    LAST_RESULT.clear()
    LAST_RESULT.update(result)


def published_result() -> dict[str, Any]:
    """A copy of the latest evaluation, for `solve_diagnostics`."""
    return dict(LAST_RESULT)


def _log_inert_once(reason: str, message: str) -> None:
    if reason in _INERT_LOGGED:
        return
    _INERT_LOGGED.add(reason)
    solver_shared._LOGGER.debug(
        "Nimbus #1784: solar running-low gate inert: %s", message
    )


def _turn_off(reason: str, detail: str) -> None:
    # Any reading that is not a judged low ends a confirmation window.
    _STATE.pending_since = None
    if not _STATE.active:
        return
    _STATE.active = False
    _STATE.on_since = None
    solver_shared._LOGGER.info(
        "Nimbus #1784: solar running low OFF (%s): %s. The rest of today is "
        "planned on the full forecast again.",
        reason,
        detail,
    )


def solcast_p50_at(
    points: list[tuple[datetime, float]], when: datetime
) -> float | None:
    """Solcast's p50 for the 30-minute period containing `when`, or None."""
    for start, p50 in points:
        if start <= when < start + SOLCAST_PERIOD:
            return p50
    return None


def decide_hysteresis(
    *,
    was_active: bool,
    actual_kw: float,
    reference_kw: float,
    on_fraction: float,
    off_fraction: float,
) -> bool:
    """The hysteresis rule, pure: turn on below `on_fraction` of the
    reference; once on, stay on until above `off_fraction`."""
    if was_active:
        return actual_kw < off_fraction * reference_kw
    return actual_kw < on_fraction * reference_kw


def confirm_low(
    *,
    low: bool,
    now: datetime,
    pending_since: datetime | None,
    confirm: timedelta,
) -> bool:
    """The confirmation rule, pure: a low reading turns the gate on only
    when confirmation is off (0) or the current unbroken run of low readings
    began at least `confirm` ago. The caller starts the run at the first low
    reading and ends it at any reading that is not low."""
    if not low:
        return False
    if confirm <= timedelta(0):
        return True
    return pending_since is not None and now - pending_since >= confirm


def ratio_scale(ratio: float) -> float:
    """Option (b)'s multiplier on p50, for a member with no lower bound:
    the measured delivery ratio, clamped to [RATIO_FLOOR, 1.0]."""
    return min(1.0, max(RATIO_FLOOR, ratio))


def read_solcast_points(sw: Any) -> tuple[list[tuple[datetime, float]], bool]:
    """(period_start, p50) for every Solcast detailedForecast period, and
    whether every one of them also carries a p10. Reads Solcast's own two
    entities (the same ones the auto-include path reads). A missing or
    malformed forecast reads as no points."""
    points: list[tuple[datetime, float]] = []
    has_p10 = True
    try:
        for eid in sorted(sw._KNOWN_SOLCAST_SOLAR_ENTITY_IDS):
            if not sw.entity_exists(eid):
                continue
            attrs = sw.ha_get(eid).get("attributes") or {}
            for p in attrs.get("detailedForecast") or []:
                points.append(
                    (sw.parse_iso(p["period_start"]), float(p["pv_estimate"]))
                )
                p10 = p.get("pv_estimate10")
                if p10 is None or isinstance(p10, bool):
                    has_p10 = False
                else:
                    float(p10)
    except Exception:  # noqa: BLE001 -- a bad or unreadable forecast is "no Solcast", never a crash
        solver_shared._LOGGER.debug(
            "Nimbus #1784: could not read Solcast's detailedForecast", exc_info=True
        )
        return [], False
    points.sort(key=lambda x: x[0])
    return points, bool(points) and has_p10


def _remember_blend(now: datetime, blend_now_kw: float | None) -> None:
    if blend_now_kw is None:
        return
    mem = _STATE.blend_memory
    mem.append((now, float(blend_now_kw)))
    cutoff = now - BLEND_MEMORY_SPAN
    mem[:] = [(t, v) for t, v in mem if t >= cutoff]


def _blend_at(when: datetime) -> float | None:
    """The latest remembered blended p50 at or before `when`, if it is no
    more than BLEND_LAG_TOLERANCE older than `when`."""
    best: tuple[datetime, float] | None = None
    for t, v in _STATE.blend_memory:
        if when - BLEND_LAG_TOLERANCE <= t <= when and (best is None or t > best[0]):
            best = (t, v)
    return best[1] if best is not None else None


def recorder_behind_by(
    sw: Any, sensor: str, hist: list[tuple[datetime, float]]
) -> timedelta | None:
    """How far the recorder's newest change for `sensor` trails the live
    sensor's own last change, or None when it does not (or either side is
    unknown). See the module docstring: a backup locks the recorder."""
    if not hist:
        return None
    try:
        live = sw.ha_get(sensor).get("last_changed")
    except Exception:  # noqa: BLE001 -- unknown is "not behind", never a crash
        solver_shared._LOGGER.debug(
            "Nimbus #1784: live state read failed for %s", sensor, exc_info=True
        )
        return None
    if not live:
        return None
    try:
        live_at = solver_shared.parse_iso(live)
    except (TypeError, ValueError):
        return None
    lag = live_at - max(t for t, _v in hist)
    return lag if lag > RECORDER_BEHIND_AFTER else None


def actual_30min_avg_kw(
    sw: Any, sensor: str, now: datetime
) -> tuple[float | None, timedelta | None]:
    """(time-weighted mean of `sensor` over [now - 30 min, now] in kW from
    the recorder, how far the recorder is behind the live sensor). The mean is
    None when the recorder returns nothing for the window, and also when it
    is behind -- a frozen mean is not a reading."""
    start = now - ACTUAL_WINDOW
    try:
        hist = sw.fetch_entity_history_range(sensor, start, now)
    except Exception:  # noqa: BLE001 -- history is optional; no history means off
        solver_shared._LOGGER.debug(
            "Nimbus #1784: recorder history read failed for %s", sensor, exc_info=True
        )
        return None, None
    if not hist:
        return None, None
    behind = recorder_behind_by(sw, sensor, hist)
    if behind is not None:
        return None, behind
    # resample_history_mean() is the repo's own time-weighted mean (#1008):
    # each recorded state holds until the next, and the state carried in
    # from before the window (HA returns the start-time state) covers the
    # lead-in. A PV sensor that held one value all window is one row, which
    # is a real reading of that value, not a gap (#1463).
    mean = sw.resample_history_mean(
        hist, [start], ACTUAL_WINDOW.total_seconds() / 3600.0
    )[0]
    scale = float(sw._kw_scale_factor(sensor))
    return max(0.0, float(mean) * scale), None


def evaluate_gate(
    sw: Any,
    cfg: dict[str, Any],
    now: datetime,
    blend_now_kw: float | None,
) -> tuple[dict[str, Any], bool]:
    """Judge the gate for this solve, update the held state, log a change,
    and return (the evidence dict, whether Solcast's p10 is available).
    `blend_now_kw` is the pre-gate blended forecast for now, used as the
    reference only when Solcast is absent. Never raises. `sw` is the
    solver_writer module (see the note above the settings)."""
    min_p50 = sw._cfg_num(cfg, CFG_MIN_P50_KW, DEFAULT_MIN_P50_KW)
    if min_p50 <= 0:
        _turn_off("off", "switched off (minimum p50 set to 0)")
        return _gate_result("off"), False
    sensor = cfg.get("solver_solar_power_sensor")
    if not sensor:
        _log_inert_once("no_solar_power_sensor", "solver_solar_power_sensor is not set")
        _turn_off("no_solar_power_sensor", "no solar power sensor")
        return _gate_result("no_solar_power_sensor"), False

    points, solcast_p10 = read_solcast_points(sw)
    _remember_blend(now, blend_now_kw)
    if points:
        source = REFERENCE_SOLCAST
        p50_now = solcast_p50_at(points, now)
        p50_lag = solcast_p50_at(points, now - REFERENCE_LAG)
    else:
        source = REFERENCE_BLEND
        p50_now = blend_now_kw
        p50_lag = _blend_at(now - REFERENCE_LAG)
    if p50_now is None or p50_lag is None:
        _turn_off("no_reference", f"no {source} forecast for now and 30 min ago")
        return (
            _gate_result(
                "no_reference",
                reference_source=source,
                p50_now_kw=p50_now,
                p50_lagged_kw=p50_lag,
            ),
            solcast_p10,
        )
    if p50_now < min_p50 or p50_lag < min_p50:
        _turn_off(
            "below_minimum",
            f"p50 now {p50_now:.2f} kW, 30 min earlier {p50_lag:.2f} kW, "
            f"below the {min_p50:.2f} kW minimum",
        )
        return (
            _gate_result(
                "below_minimum",
                reference_source=source,
                p50_now_kw=p50_now,
                p50_lagged_kw=p50_lag,
            ),
            solcast_p10,
        )

    actual, behind = actual_30min_avg_kw(sw, sensor, now)
    if behind is not None:
        _turn_off(
            "recorder_behind",
            f"the recorder's newest {sensor} change is "
            f"{behind.total_seconds() / 60:.0f} min behind the live sensor "
            "(a backup locks it)",
        )
        return (
            _gate_result(
                "recorder_behind",
                reference_source=source,
                p50_now_kw=p50_now,
                p50_lagged_kw=p50_lag,
            ),
            solcast_p10,
        )
    if actual is None:
        _turn_off("no_history", f"no recorder history for {sensor}")
        return (
            _gate_result(
                "no_history",
                reference_source=source,
                p50_now_kw=p50_now,
                p50_lagged_kw=p50_lag,
            ),
            solcast_p10,
        )

    on_pct = sw._cfg_num(cfg, CFG_ON_PERCENT, DEFAULT_ON_PERCENT)
    # An "off" threshold below "on" would flap every solve; treat it as
    # equal (no dead band) rather than as a configuration error.
    off_pct = max(on_pct, sw._cfg_num(cfg, CFG_OFF_PERCENT, DEFAULT_OFF_PERCENT))
    reference = min(p50_now, p50_lag)
    was_active = _STATE.active
    active = decide_hysteresis(
        was_active=was_active,
        actual_kw=actual,
        reference_kw=reference,
        on_fraction=on_pct / 100.0,
        off_fraction=off_pct / 100.0,
    )
    if not was_active:
        # While off, `active` here is "this reading is low"; the
        # confirmation window decides whether it turns the gate on.
        low = active
        if not low:
            _STATE.pending_since = None
        elif _STATE.pending_since is None:
            _STATE.pending_since = now
        confirm = timedelta(
            minutes=max(
                0.0, sw._cfg_num(cfg, CFG_CONFIRM_MINUTES, DEFAULT_CONFIRM_MINUTES)
            )
        )
        active = confirm_low(
            low=low, now=now, pending_since=_STATE.pending_since, confirm=confirm
        )
        if active:
            _STATE.pending_since = None
    ratio = actual / reference if reference > 0 else 0.0
    if active and not was_active:
        _STATE.active = True
        _STATE.on_since = now
        solver_shared._LOGGER.warning(
            "Nimbus #1784: solar running LOW -- actual PV averaged %.2f kW over "
            "the last 30 min against a %s p50 reference of %.2f kW (now "
            "%.2f kW, 30 min earlier %.2f kW): ratio %.2f, below %.0f%%. The "
            "rest of today is planned on the lower forecast.",
            actual,
            source,
            reference,
            p50_now,
            p50_lag,
            ratio,
            on_pct,
        )
    elif was_active and not active:
        _turn_off(
            "recovered",
            f"actual PV averaged {actual:.2f} kW against a reference of "
            f"{reference:.2f} kW (now {p50_now:.2f}, 30 min earlier "
            f"{p50_lag:.2f}): ratio {ratio:.2f}, above {off_pct:.0f}%",
        )
    return (
        _gate_result(
            "judged",
            reference_source=source,
            actual_kw=actual,
            p50_now_kw=p50_now,
            p50_lagged_kw=p50_lag,
        ),
        solcast_p10,
    )


def remaining_today_mask(grid_times: list[datetime], now: datetime) -> list[bool]:
    """True for the periods the gate lowers: every period after the current
    one (index 0, which the live anchor owns) that starts on today's local
    date. `now` carries the local timezone (main() builds it in LOCAL_TZ)."""
    today = now.date()
    tz = now.tzinfo
    return [
        i >= 1 and gt.astimezone(tz).date() == today for i, gt in enumerate(grid_times)
    ]


def member_method(
    *,
    is_solcast: bool,
    solcast_p10: bool,
    value: list[float],
    lower: list[float],
    idx: list[int],
) -> str:
    """How one blend member is lowered while the gate is on (see the module
    docstring): Solcast with p10 -> its p10; a member whose lower bound sits
    genuinely below its value somewhere in today's remaining periods -> that
    lower bound; anything else -> option (b), ratio-scaled."""
    if is_solcast:
        return METHOD_P10 if solcast_p10 else METHOD_RATIO
    if any(lower[i] < value[i] - 1e-9 for i in idx):
        return METHOD_LOWER_BOUND
    return METHOD_RATIO
