"""Distrust a P5MIN price spike that an infeasible run produced, or that
dispatch contradicted (nimbus #1778, Mark Purcell).

On 15 Oct 2026 at 15:50 AEMO invoked a Brisbane control-room constraint
(`CA_BRIS_...`). P5MIN applied it with a right-hand side about half of
dispatch's, could not meet it, and priced QLD1 at the $23,200/MWh cap in
every interval of every run from 15:50 to 16:30. Dispatch met the same
constraint and never went above $295/MWh. Amber passed the cap through as
its forecast ($26.31/kWh import, $23.86 feed-in, 16:15-17:59), and an
optimiser reading it charged from the grid to sell into a spike that stayed
one interval ahead until it vanished.

Two rules raise the flag, kept in `FalseSpikeGuard`:

* **(a) infeasible spike** -- a run with an interval at or above the spike
  threshold AND at least one violated generic constraint
  (CONSTRAINTSOLUTION `VIOLATIONDEGREE > 0`). A `CA_` control-room
  constraint is preferred as the one reported, not required.
* **(b) dispatch disagrees** -- the forecast recorded for an interval end
  (the last run that carried it before dispatch) was a spike, and dispatch
  for that interval came in below $1,000/MWh AND below 30% of the forecast.

It releases on a dispatch interval at or above the threshold, on a run with
no spike interval left, or 60 minutes after it was raised. After a timeout
or a real dispatch spike it does not re-arm until a run is clean again, so
the 60 minutes bound one episode rather than restarting every run.

While it holds, `guard_reading()` replaces the solver's forward spike
prices from the GUARDED_SOURCES (Amber Express, Amber Electric, and the
P5MIN sensor itself) with the source's live price, or a non-spike fallback.
The current 5-minute interval keeps its real price, so a spike that does
arrive in dispatch is still sold into. LocalVolts is not in the set: its
forecast never carried the cap on 15 Oct.

Pure: no `homeassistant` import. The sensor (`sensor_p5min.py`) holds the
guard and publishes it as the `false_spike_guard` attribute; the solver
reads that attribute.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

try:
    from . import p5min, price_intervals
except ImportError:  # pragma: no cover - standalone/cron path
    import p5min  # type: ignore[no-redef]
    import price_intervals  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

ATTRIBUTE = "false_spike_guard"
P5MIN_ENTITY_ID = "sensor.nimbus_p5min_forecast"

# "At or above $5,000/MWh, or within 10% of the cap", the lower of the two
# (#1778). The $/MWh figure is the dashboard number; the cap is AEMO's
# Market Price Cap for 1 Jul 2026 - 30 Jun 2027, the value P5MIN printed on
# 15 Oct.
DEFAULT_THRESHOLD_MWH = 5000.0
MARKET_PRICE_CAP_MWH = 23200.0
CAP_PROXIMITY = 0.9
# Rule (b): dispatch below $1,000/MWh and below 30% of the forecast.
DISPATCH_CEILING_KWH = 1.0
DISPATCH_FRACTION = 0.30
MAX_AGE = timedelta(minutes=60)

REASON_INFEASIBLE = "infeasible_spike"
REASON_DISPATCH = "dispatch_disagrees"
RELEASE_DISPATCH_SPIKE = "dispatch_spiked"
RELEASE_CLEAN = "forecast_clean"
RELEASE_TIMEOUT = "timeout"

SOURCE_AMBER_EXPRESS = "amber_express"
SOURCE_AMBER_ELECTRIC = "amber_electric"
SOURCE_P5MIN = "nimbus_p5min"
# Which forward prices the flag rewrites. LocalVolts is deliberately absent
# (#1778: its forecast did not carry the cap). Add a source here to guard it.
GUARDED_SOURCES = frozenset({SOURCE_AMBER_EXPRESS, SOURCE_AMBER_ELECTRIC, SOURCE_P5MIN})

_INTERVAL = timedelta(minutes=5)
_KEEP_FORECASTS = 24  # two hours of interval ends
_KEEP_COMPARED = 12


def threshold_kwh(threshold_mwh: float | None) -> float:
    """The spike threshold in $/kWh: the configured $/MWh, or 90% of the
    cap when that is lower. A missing or non-positive setting is the
    default."""
    t = threshold_mwh if threshold_mwh and math.isfinite(threshold_mwh) else 0.0
    if t <= 0:
        t = DEFAULT_THRESHOLD_MWH
    return min(t, CAP_PROXIMITY * MARKET_PRICE_CAP_MWH) / 1000.0


def has_spike(rows: list[dict[str, Any]], threshold: float) -> bool:
    return any(r.get("value", -math.inf) >= threshold for r in rows or [])


def _headline(run: dict[str, Any]) -> dict[str, Any] | None:
    """The violated constraint to report: the first interval's, a `CA_`
    one preferred, then the largest violation."""
    rows = p5min.first_interval_violations(run) or run.get("violated") or []
    ca = [v for v in rows if str(v.get("constraint_id", "")).startswith("CA_")]
    pool = ca or rows
    return max(pool, key=lambda v: v["violation"]) if pool else None


def infeasible_spike(
    run: dict[str, Any] | None, threshold: float
) -> dict[str, Any] | None:
    """Rule (a): the detail of a run that forecasts a spike while violating
    a generic constraint, or None."""
    rows = (run or {}).get("forecast") or []
    headline = _headline(run or {})
    if headline is None or not has_spike(rows, threshold):
        return None
    top = p5min.peak(rows) or {}
    return {
        **headline,
        "forecast_peak": top.get("value"),
        "forecast_peak_start": top.get("start"),
    }


def dispatch_disagrees(forecast: float, dispatch: float, threshold: float) -> bool:
    """Rule (b) for one interval: a spike forecast, and dispatch below
    $1,000/MWh and below 30% of it."""
    return (
        forecast >= threshold
        and dispatch < DISPATCH_CEILING_KWH
        and dispatch < DISPATCH_FRACTION * forecast
    )


def interval_end(when: datetime) -> datetime:
    """The end of the 5-minute interval `when` falls in."""
    floored = when.replace(second=0, microsecond=0) - timedelta(minutes=when.minute % 5)
    return floored + _INTERVAL


class FalseSpikeGuard:
    """The flag's state. Fed P5MIN runs (`on_run`), dispatch prices
    (`on_dispatch`) and the clock (`expire`)."""

    def __init__(
        self, threshold_mwh: float = DEFAULT_THRESHOLD_MWH, max_age: timedelta = MAX_AGE
    ) -> None:
        self.threshold_mwh = threshold_mwh
        self.max_age = max_age
        self.since: datetime | None = None
        self.reason: str | None = None
        self.detail: dict[str, Any] = {}
        self.latched = False
        self.last_release: dict[str, str] | None = None
        self.forecast_by_end: dict[str, float] = {}
        self.compared: list[dict[str, Any]] = []

    @property
    def threshold(self) -> float:
        return threshold_kwh(self.threshold_mwh)

    @property
    def active(self) -> bool:
        return self.since is not None

    def _activate(self, reason: str, at: datetime, detail: dict[str, Any]) -> None:
        self.since, self.reason, self.detail = at, reason, detail

    def _release(self, why: str, at: datetime) -> None:
        self.last_release = {"reason": why, "at": at.isoformat()}
        self.since, self.reason, self.detail = None, None, {}

    def expire(self, now: datetime | None) -> None:
        """Release after `max_age`, and hold off re-arming until a clean run."""
        if (
            self.since is not None
            and now is not None
            and now - self.since >= self.max_age
        ):
            self._release(RELEASE_TIMEOUT, now)
            self.latched = True

    def on_run(self, run: dict[str, Any] | None, published: datetime | None) -> None:
        rows = (run or {}).get("forecast") or []
        if not rows:
            return
        for row in rows:
            self.forecast_by_end[row["end"]] = row["value"]
        for old in sorted(self.forecast_by_end)[:-_KEEP_FORECASTS]:
            del self.forecast_by_end[old]
        self.expire(published)
        if not has_spike(rows, self.threshold):
            if self.active and published is not None:
                self._release(RELEASE_CLEAN, published)
            self.latched = False
            return
        if self.active or self.latched or published is None:
            return
        hit = infeasible_spike(run, self.threshold)
        if hit is not None:
            self._activate(REASON_INFEASIBLE, published, hit)

    def on_dispatch(self, end: datetime, price: float, now: datetime) -> None:
        """One dispatch price ($/kWh) for the interval ending `end`. Keyed
        in NEM time, as P5MIN's own interval ends are."""
        key = end.astimezone(p5min.NEM_TIME).isoformat()
        if self.compared and self.compared[-1]["end"] == key:
            return
        forecast = self.forecast_by_end.get(key)
        self.compared.append(
            {"end": key, "forecast": forecast, "dispatch": round(price, 6)}
        )
        del self.compared[:-_KEEP_COMPARED]
        self.expire(now)
        if price >= self.threshold:
            if self.active:
                self._release(RELEASE_DISPATCH_SPIKE, now)
            self.latched = True
            return
        if self.active or self.latched or forecast is None:
            return
        if dispatch_disagrees(forecast, price, self.threshold):
            self._activate(
                REASON_DISPATCH,
                now,
                {"interval_end": key, "forecast": forecast, "dispatch": price},
            )

    def attribute(self) -> dict[str, Any]:
        expires = self.since + self.max_age if self.since is not None else None
        return {
            "active": self.active,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "expires_at": expires.isoformat() if expires else None,
            "threshold": round(self.threshold, 4),
            "detail": dict(self.detail),
            "latched": self.latched,
            "last_release": self.last_release,
        }

    def describe(self, run: dict[str, Any] | None) -> str:
        """One log line for a run while the flag holds."""
        head = _headline(run or {}) or {}
        return (
            f"Nimbus #1778: P5MIN run {(run or {}).get('run_datetime')} "
            f"distrusted ({self.reason} since {self.since.isoformat() if self.since else None}): "
            f"constraint {head.get('constraint_id')} RHS {head.get('rhs')} MW violated by "
            f"{head.get('violation')} MW; forward spike prices >= "
            f"{self.threshold:.2f}/kWh from {', '.join(sorted(GUARDED_SOURCES))} "
            f"are replaced until it releases"
        )


# ---- solver side ----------------------------------------------------------


def holds(attr: Any, now: datetime) -> bool:
    """Whether a published `false_spike_guard` attribute is active at `now`.
    The solver checks `expires_at` itself, so a sensor that stopped updating
    cannot hold the flag past its timeout."""
    if not isinstance(attr, dict) or not attr.get("active"):
        return False
    try:
        expires = datetime.fromisoformat(str(attr.get("expires_at")))
    except ValueError:
        return False
    return expires.tzinfo is not None and now < expires


def source_kind(entity_id: str, attrs: Any) -> str | None:
    """Which provider a price sensor is, from its own attributes."""
    if entity_id == P5MIN_ENTITY_ID:
        return SOURCE_P5MIN
    if not isinstance(attrs, dict):
        return None
    if price_intervals.AMBER_EXPRESS_DETAILED_KEY in attrs:
        return SOURCE_AMBER_EXPRESS
    if price_intervals.is_amber_core_rows(
        attrs.get(price_intervals.AMBER_CORE_ROWS_KEY)
    ):
        return SOURCE_AMBER_ELECTRIC
    return None


def replace_spikes(
    values: list[float],
    grid_times: list[datetime],
    threshold: float,
    live_price: float | None,
) -> tuple[list[float], int, float | None]:
    """(values, replaced, fallback). Periods inside the current 5-minute
    interval keep their price. Later periods at or above `threshold` take
    the live price when it is not a spike, else the current interval's own
    non-spike price, else the median of the series' non-spike prices. With
    no non-spike price anywhere, nothing is replaced."""
    if not values or not grid_times:
        return list(values), 0, None
    keep_until = interval_end(grid_times[0])
    current = [
        v for v, t in zip(values, grid_times) if t < keep_until and v < threshold
    ]
    calm = sorted(v for v in values if v < threshold)
    if live_price is not None and math.isfinite(live_price) and live_price < threshold:
        fallback: float | None = live_price
    elif current:
        fallback = current[0]
    elif calm:
        fallback = calm[len(calm) // 2]
    else:
        fallback = None
    if fallback is None:
        return list(values), 0, None
    out = list(values)
    replaced = 0
    for i, (v, t) in enumerate(zip(values, grid_times)):
        if t >= keep_until and v >= threshold:
            out[i] = fallback
            replaced += 1
    return out, replaced, fallback


def _state_price(state: dict[str, Any]) -> float | None:
    try:
        value = float(state["state"])
    except (KeyError, TypeError, ValueError):
        return None
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    return value * price_intervals.price_unit_scale(unit)


_LOGGED: set[tuple[str, str]] = set()


def guard_reading(
    entity_id: str,
    state: dict[str, Any],
    reading: tuple[list[float], list[bool]] | None,
    grid_times: list[datetime],
    threshold_mwh: float | None,
    read_state: Callable[[str], dict[str, Any]],
) -> tuple[list[float], list[bool]] | None:
    """A price reader's (values, real) after the guard. Unchanged unless
    `threshold_mwh` is given, the sensor is one of GUARDED_SOURCES, and the
    P5MIN sensor's flag holds. Never raises: a failed read of the flag is
    no flag."""
    if reading is None or threshold_mwh is None or not grid_times:
        return reading
    attrs = state.get("attributes") if isinstance(state, dict) else None
    if source_kind(entity_id, attrs) not in GUARDED_SOURCES:
        return reading
    try:
        flag = (read_state(P5MIN_ENTITY_ID).get("attributes") or {}).get(ATTRIBUTE)
    except Exception:  # noqa: BLE001 -- no P5MIN sensor, no flag
        return reading
    if not isinstance(flag, dict) or not holds(flag, grid_times[0]):
        return reading
    values, replaced, fallback = replace_spikes(
        list(reading[0]), grid_times, threshold_kwh(threshold_mwh), _state_price(state)
    )
    if replaced:
        key = (str(flag.get("since")), entity_id)
        if key not in _LOGGED:
            if len(_LOGGED) > 64:
                _LOGGED.clear()
            _LOGGED.add(key)
            _LOGGER.warning(
                "Nimbus #1778: %s: %d forward spike period(s) replaced with %.4f/kWh "
                "while the P5MIN false-spike flag holds (%s since %s, until %s)",
                entity_id,
                replaced,
                fallback,
                flag.get("reason"),
                flag.get("since"),
                flag.get("expires_at"),
            )
    return values, reading[1]
