"""Solar "running low" gate (nimbus issue #1784, Mark Purcell).

On overcast days every day-ahead PV forecast the reference households track
runs far too high, Solcast's p50 included: on 15 Oct 2026 actual PV was
38.3 kWh against a p50 of 83.2 kWh (+117%). Overcast days land at roughly
45-70% of p50, close to Solcast's own p10. A plan built on p50 holds the
battery for solar that never arrives.

So, as Mark specified on the issue (option (a), the gate his ha-config runs
for HAEO since 16 Oct 2026):

- Measure actual PV as a 30-minute mean of `solver_solar_power_sensor`.
- Compare it with a REFERENCE: the lower of Solcast's p50 for the current
  30-minute period and for the period 30 minutes earlier. A 30-minute mean
  trails a rising morning curve by about its own length, so against the
  current p50 alone it trips every dawn (0.07 kW against 1.10 kW at 06:20 on
  his install). The lagged value fixes the morning; taking the LOWER of the
  two also stops a falling afternoon curve, where the mean lags the other
  way, reading as low output.
- Judge only while both p50s are at least a minimum (2 kW by default). The
  first hour of production runs low and noisy against Solcast.
- Hysteresis: on below 70% of the reference, off above 90%, so passing
  clouds do not make it flap.
- While on, the remaining periods of TODAY plan on Solcast p10 in place of
  p50. Later days are untouched: same-day evidence says nothing about
  tomorrow.

Whenever the gate cannot judge (switched off, no Solcast p10, no PV sensor,
no reference period, below the minimum, no history) it is OFF -- exactly
the ha-config template, which returns false on every one of those. So the
gate also switches itself off each evening once p50 falls under the
minimum, and "today only" needs no date bookkeeping.

## Why recorder history and not an in-process EMA

ha-config's average is a template EMA updated every minute. That is the
right tool in HA YAML, where a new integration needs a restart. Here the
mean is computed from the recorder's own history of the PV sensor on every
solve instead, for one reason: it is correct straight after a restart. An
EMA held in this process would start from nothing (or from 0 W, the way the
template does overnight) and read low for its first half hour -- exactly
the "running low" signature -- after every HA restart or Nimbus reload.
The recorder already holds the last 30 minutes, so nothing has to warm up.
If that history cannot be read, the gate is off: a guessed average is the
one input this gate must never act on.

The gate's ON/OFF state itself is held in this process between solves, as
the issue allows. After a restart it starts off and re-judges at the next
solve, from the recorder's history -- so a genuinely dull day turns it back
on at the first solve rather than after a warm-up.

## How the plan changes

`build_solar_arrays()` asks `evaluate_gate()` for a decision before it blends.
When the gate is on, each Solcast member of the blend has its value
replaced by its own lower bound (Solcast's p10, `pv_estimate10`) for the
remaining periods of today, BEFORE blending, the confidence band, the live
anchor at index 0, the #1259 nowcast measurement and the #128 delivery
ratio. Other blend members (Open-Meteo, a configured generic source) are
not touched: the gate measures Solcast and only Solcast's own p10 is the
evidence-backed lower forecast.

Prior art (per this repo's CHECK PRIOR ART directive): HAEO itself has
none -- this is purcell-lab/ha-config's template around HAEO, ported. EMHASS:
`pv_bias_calibration.py` tunes a P10/P50 blend weight once per resolved DAY,
with no same-day switch; neither has an intraday running-low gate.

Never raises: every failure path is a "not judged" result with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass
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
# 0 = off, the repo's convention for a feature carried by a number.
DEFAULT_MIN_P50_KW = 2.0
DEFAULT_ON_PERCENT = 70.0
DEFAULT_OFF_PERCENT = 90.0

# Solcast's detailedForecast is 30-minute periods; the average spans the
# same length, which is why the lagged reference is one period back.
SOLCAST_PERIOD = timedelta(minutes=30)
ACTUAL_WINDOW = timedelta(minutes=30)
REFERENCE_LAG = timedelta(minutes=30)


@dataclass
class _GateState:
    active: bool = False
    on_since: datetime | None = None


# In-process, by design (see the module docstring). Starts off.
_STATE = _GateState()
# Log-once set for the "inert on this install" DEBUG lines.
_INERT_LOGGED: set[str] = set()


def reset_gate_state() -> None:
    """Back to a fresh process: gate off, log-once set empty."""
    _STATE.active = False
    _STATE.on_since = None
    _INERT_LOGGED.clear()


def _gate_result(
    reason: str,
    *,
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
        "actual_30min_avg_kw": r(actual_kw),
        "p50_now_kw": r(p50_now_kw),
        "p50_lagged_kw": r(p50_lagged_kw),
        "reference_kw": r(reference),
        "ratio": round(ratio, 4) if ratio is not None else None,
        "on_since": _STATE.on_since.isoformat() if _STATE.on_since else None,
        # Filled in by build_solar_arrays() once it knows how many periods
        # it switched; 0 while off.
        "periods_on_p10": 0,
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
    if not _STATE.active:
        return
    _STATE.active = False
    _STATE.on_since = None
    solver_shared._LOGGER.info(
        "Nimbus #1784: solar running low OFF (%s): %s. The rest of today is "
        "planned on Solcast p50 again.",
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


def read_solcast_points(sw: Any) -> tuple[list[tuple[datetime, float]], bool]:
    """(period_start, p50) for every Solcast detailedForecast period, and
    whether every one of them also carries a p10. Reads Solcast's own two
    entities (the same ones the auto-include path reads). A missing,
    malformed or p10-less forecast reads as "no p10", which makes the gate
    inert: _solar_entries_from_attributes() fills a missing pv_estimate10
    with 0.0, and planning on that would zero today's solar."""
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
    except Exception:  # noqa: BLE001 -- a bad or unreadable forecast is "no p10", never a crash
        solver_shared._LOGGER.debug(
            "Nimbus #1784: could not read Solcast's detailedForecast", exc_info=True
        )
        return [], False
    points.sort(key=lambda x: x[0])
    return points, bool(points) and has_p10


def actual_30min_avg_kw(sw: Any, sensor: str, now: datetime) -> float | None:
    """Time-weighted mean of `sensor` over [now - 30 min, now], in kW, from
    the recorder. None when the recorder returns nothing for the window."""
    start = now - ACTUAL_WINDOW
    try:
        hist = sw.fetch_entity_history_range(sensor, start, now)
    except Exception:  # noqa: BLE001 -- history is optional; no history means off
        solver_shared._LOGGER.debug(
            "Nimbus #1784: recorder history read failed for %s", sensor, exc_info=True
        )
        return None
    if not hist:
        return None
    # resample_history_mean() is the repo's own time-weighted mean (#1008):
    # each recorded state holds until the next, and the state carried in
    # from before the window (HA returns the start-time state) covers the
    # lead-in. A PV sensor that held one value all window is one row, which
    # is a real reading of that value, not a gap (#1463).
    mean = sw.resample_history_mean(
        hist, [start], ACTUAL_WINDOW.total_seconds() / 3600.0
    )[0]
    scale = float(sw._kw_scale_factor(sensor))
    return max(0.0, float(mean) * scale)


def evaluate_gate(
    sw: Any, cfg: dict[str, Any], now: datetime, solcast_in_blend: bool
) -> dict[str, Any]:
    """Judge the gate for this solve, update the held state, log a change,
    and return the evidence dict. Never raises. `sw` is the solver_writer
    module (see the note above the settings)."""
    min_p50 = sw._cfg_num(cfg, CFG_MIN_P50_KW, DEFAULT_MIN_P50_KW)
    if min_p50 <= 0:
        _turn_off("off", "switched off (minimum p50 set to 0)")
        return _gate_result("off")
    if not solcast_in_blend:
        _log_inert_once(
            "no_solcast",
            "no Solcast forecast is in the solar blend (another solar source)",
        )
        _turn_off("no_solcast", "no Solcast forecast in the blend")
        return _gate_result("no_solcast")
    points, has_p10 = read_solcast_points(sw)
    if not has_p10:
        _log_inert_once(
            "no_solcast_p10",
            "Solcast's detailedForecast carries no pv_estimate10 (p10) here",
        )
        _turn_off("no_solcast_p10", "Solcast p10 unavailable")
        return _gate_result("no_solcast_p10")
    sensor = cfg.get("solver_solar_power_sensor")
    if not sensor:
        _log_inert_once("no_solar_power_sensor", "solver_solar_power_sensor is not set")
        _turn_off("no_solar_power_sensor", "no solar power sensor")
        return _gate_result("no_solar_power_sensor")

    p50_now = solcast_p50_at(points, now)
    p50_lag = solcast_p50_at(points, now - REFERENCE_LAG)
    if p50_now is None or p50_lag is None:
        _turn_off("no_reference", "no Solcast period covers now or 30 min ago")
        return _gate_result("no_reference", p50_now_kw=p50_now, p50_lagged_kw=p50_lag)
    if p50_now < min_p50 or p50_lag < min_p50:
        _turn_off(
            "below_minimum",
            f"p50 now {p50_now:.2f} kW, 30 min earlier {p50_lag:.2f} kW, "
            f"below the {min_p50:.2f} kW minimum",
        )
        return _gate_result("below_minimum", p50_now_kw=p50_now, p50_lagged_kw=p50_lag)

    actual = actual_30min_avg_kw(sw, sensor, now)
    if actual is None:
        _turn_off("no_history", f"no recorder history for {sensor}")
        return _gate_result("no_history", p50_now_kw=p50_now, p50_lagged_kw=p50_lag)

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
    ratio = actual / reference if reference > 0 else 0.0
    if active and not was_active:
        _STATE.active = True
        _STATE.on_since = now
        solver_shared._LOGGER.warning(
            "Nimbus #1784: solar running LOW -- actual PV averaged %.2f kW over "
            "the last 30 min against a Solcast p50 reference of %.2f kW (now "
            "%.2f kW, 30 min earlier %.2f kW): ratio %.2f, below %.0f%%. The "
            "rest of today is planned on Solcast p10.",
            actual,
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
    return _gate_result(
        "judged", actual_kw=actual, p50_now_kw=p50_now, p50_lagged_kw=p50_lag
    )


def remaining_today_mask(grid_times: list[datetime], now: datetime) -> list[bool]:
    """True for the periods p10 replaces: every period after the current
    one (index 0, which the live anchor owns) that starts on today's local
    date. `now` carries the local timezone (main() builds it in LOCAL_TZ)."""
    today = now.date()
    tz = now.tzinfo
    return [
        i >= 1 and gt.astimezone(tz).date() == today for i, gt in enumerate(grid_times)
    ]
