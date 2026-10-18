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

Four rules raise the flag, kept in `FalseSpikeGuard`:

* **(a) infeasible spike** -- a run with an interval at or above the spike
  threshold AND at least one violated generic constraint
  (CONSTRAINTSOLUTION `VIOLATIONDEGREE > 0`). A `CA_` control-room
  constraint is preferred as the one reported, not required.
* **(b) dispatch disagrees** -- the forecast recorded for an interval end
  (the last run that carried it before dispatch) was a spike, and dispatch
  for that interval came in below $1,000/MWh AND below 30% of the forecast.
* **(c) control-room constraint the forecast cannot meet** -- the latest
  run's first interval violates a `CA_` (AEMO control-room constraint
  automation) constraint while the latest dispatch price is normal (below
  $1,000/MWh), even when P5MIN itself prices nothing high. On 16 Oct 2026
  P5MIN violated `CA_BRIS_59E3F2B6_1` by 560-980 MW from the 09:55 run at
  ~9c/kWh, while the 30-minute pre-dispatch priced 13:00-16:00 at
  $9-23/kWh and dispatch met the same constraint at ~$60/MWh (Mark Purcell,
  #1778). The spike sat beyond P5MIN's horizon, so (a) could not fire until
  it was too late to plan around. Measured on the fixtures: every 15 Oct
  spiked run violates a `CA_BRIS` constraint and the first clean run does
  not; no 9 Oct run (a real spike) violates any `CA_` constraint.
  Only a material violation counts: RHS / (RHS + violation) at or below
  0.8. Dispatch meets the constraint at its own limit, so that figure
  approximates the forecast RHS over dispatch's, the ratio Mark measured at
  0.38-0.49 through the 16 Oct spike and 1.00 once it cleared. Measured:
  0.52-0.75 in every 15 Oct spiked run, 0.55-0.70 on 16 Oct 09:55-10:50,
  0.96 for 16 Oct 11:10's 79 MW violation at normal prices.
* **(d) control-room constraint at a fraction of dispatch's limit** --
  Mark Purcell's dispatch-side test (#1778, 16 Oct 2026): a `CA_`
  constraint binding in the latest dispatch interval (non-zero marginal
  value, no violation) carries a right-hand side in the P5MIN run's first
  interval under 0.6 of dispatch's, or the forecast violates it, while
  dispatch is normal (`dispatch_rhs.compare`'s `control_room_suspect`). It
  needs no violation in the forecast, so it covers a forecast that binds the
  wrong limit without breaking it, which (c) cannot see. Measured on every
  P5MIN run of 13-16 Oct 2026 (1,152): `CA_` rows read 0.46-0.50 in every
  false-spike run and 0.96-1.09 in all ~160 others.
  The flag changes no price on its own: the solver replaces forward prices
  only when they carry a spike, so a flag with no pending spike is inert.

It releases on a dispatch interval at or above the threshold, on a run with
no spike interval left, or 60 minutes after the last run that still met rule
(a). Each run that still forecasts the spike AND still violates a constraint,
or still meets (c) or (d), renews it: on 16 Oct 2026 the same false spike sat in the forecasts from
13:00 to 16:00, so a fixed 60 minutes from the first flag would have let go
before the hours an optimiser sells into. A spike run that no longer
violates anything does not renew it, so the 60 minutes still bound how long
an unconfirmed flag outlives its evidence. After a timeout or a real
dispatch spike it does not re-arm until a run is clean again.

While it holds, `guard_schedule_prices()` replaces forward spikes in
Nimbus's SCHEDULING prices -- the import and export arrays the plan is
solved against, after every source has been read and blended -- with the
current interval's price, or a non-spike fallback. Whatever the source:
Mark Purcell on #1779, "This isn't an Amber vs Localvolts issue. Both carry
incorrect spike forecasts... This guard should be against the Nimbus
scheduling prices." The current 5-minute interval keeps its real price, so a
spike that does arrive in dispatch is still sold into.

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
    from . import p5min
except ImportError:  # pragma: no cover - standalone/cron path
    import p5min  # type: ignore[no-redef]

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
REASON_CONTROL_ROOM = "control_room_constraint"
REASON_DISPATCH_RHS = "control_room_rhs"
# Rule (c): RHS / (RHS + violation) at or below this is a material violation.
CONTROL_ROOM_MAX_RATIO = 0.8
RELEASE_DISPATCH_SPIKE = "dispatch_spiked"
RELEASE_CLEAN = "forecast_clean"
RELEASE_TIMEOUT = "timeout"

_INTERVAL = timedelta(minutes=5)
_KEEP_FORECASTS = 24  # two hours of interval ends
_KEEP_COMPARED = 12


def threshold_kwh(threshold_mwh: float | None) -> float:
    """The spike threshold in $/kWh: the configured $/MWh, or 90% of the
    cap when that is lower. Unset (None or non-finite) is the default; 0 or
    below is OFF (infinite threshold, so nothing is ever a spike), the same
    "0 = off" convention as Charge By and the reserve (Mark's review, #1779)."""
    if threshold_mwh is None or not math.isfinite(threshold_mwh):
        threshold_mwh = DEFAULT_THRESHOLD_MWH
    if threshold_mwh <= 0:
        return math.inf
    return min(threshold_mwh, CAP_PROXIMITY * MARKET_PRICE_CAP_MWH) / 1000.0


def has_spike(rows: list[dict[str, Any]], threshold: float) -> bool:
    return any(r.get("value", -math.inf) >= threshold for r in rows or [])


def _headline(run: dict[str, Any]) -> dict[str, Any] | None:
    """The violated constraint to report: the first interval's, a `CA_`
    one preferred, then the largest violation."""
    rows = p5min.first_interval_violations(run) or run.get("violated") or []
    ca = [v for v in rows if str(v.get("constraint_id", "")).startswith("CA_")]
    pool = ca or rows
    return max(pool, key=lambda v: v["violation"]) if pool else None


def _ratio(row: dict[str, Any]) -> float | None:
    rhs, violation = row.get("rhs"), row.get("violation")
    if not isinstance(rhs, (int, float)) or not isinstance(violation, (int, float)):
        return None
    if rhs <= 0 or violation <= 0:
        return None
    return float(rhs) / (float(rhs) + float(violation))


def control_room_violation(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rule (c)'s evidence: the largest material `CA_` constraint violation
    in the run's first interval, or None. Material: RHS / (RHS + violation)
    at or below `CONTROL_ROOM_MAX_RATIO`; a row without a positive RHS does
    not count."""
    rows = [
        v
        for v in p5min.first_interval_violations(run or {}) or []
        if str(v.get("constraint_id", "")).startswith("CA_")
        and _ratio(v) is not None
        and _ratio(v) <= CONTROL_ROOM_MAX_RATIO
    ]
    return max(rows, key=lambda v: v["violation"]) if rows else None


def _rhs_evidence(rhs: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rule (d)'s detail: the lowest-ratio suspect `CA_` row of a
    `dispatch_rhs.compare()` result whose `control_room_suspect` holds (it
    already requires normal dispatch), with the dispatch price, or None."""
    if not isinstance(rhs, dict) or not rhs.get("control_room_suspect"):
        return None
    rows = [
        r
        for r in rhs.get("constraints") or []
        if r.get("suspect") and str(r.get("constraint_id", "")).startswith("CA_")
    ]
    if not rows:
        return None
    return {**rows[0], "dispatch_rrp": rhs.get("dispatch_rrp")}


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
        # The last run that met rule (a) while the flag held; the timeout
        # counts from here (or from `since` when nothing has renewed it).
        self.renewed: datetime | None = None
        self.reason: str | None = None
        self.detail: dict[str, Any] = {}
        self.latched = False
        self.last_release: dict[str, str] | None = None
        self.forecast_by_end: dict[str, float] = {}
        # The latest dispatch price ($/kWh): rule (c) needs it normal.
        self.last_dispatch: float | None = None
        self.compared: list[dict[str, Any]] = []

    @property
    def threshold(self) -> float:
        return threshold_kwh(self.threshold_mwh)

    @property
    def active(self) -> bool:
        return self.since is not None

    @property
    def expires_at(self) -> datetime | None:
        start = self.renewed or self.since
        return start + self.max_age if start is not None else None

    def _activate(self, reason: str, at: datetime, detail: dict[str, Any]) -> None:
        self.since, self.reason, self.detail = at, reason, detail
        self.renewed = None

    def _release(self, why: str, at: datetime) -> None:
        self.last_release = {"reason": why, "at": at.isoformat()}
        self.since, self.reason, self.detail = None, None, {}
        self.renewed = None

    def expire(self, now: datetime | None) -> None:
        """Release `max_age` after the flag was raised or last renewed, and
        hold off re-arming until a clean run."""
        expires = self.expires_at
        if expires is not None and now is not None and now >= expires:
            self._release(RELEASE_TIMEOUT, now)
            self.latched = True

    def on_run(
        self,
        run: dict[str, Any] | None,
        published: datetime | None,
        rhs: dict[str, Any] | None = None,
    ) -> None:
        """One P5MIN run. `rhs` is `dispatch_rhs.compare()` for the run and
        the dispatch interval before it, or None; rule (d) reads it."""
        rows = (run or {}).get("forecast") or []
        if not rows:
            return
        for row in rows:
            self.forecast_by_end[row["end"]] = row["value"]
        for old in sorted(self.forecast_by_end)[:-_KEEP_FORECASTS]:
            del self.forecast_by_end[old]
        self.expire(published)
        spike = has_spike(rows, self.threshold)
        # The threshold at 0 turns the guard off, rule (c) included.
        live = math.isfinite(self.threshold)
        control_room = control_room_violation(run) if live else None
        dispatch_rhs = _rhs_evidence(rhs) if live else None
        if not spike and control_room is None and dispatch_rhs is None:
            if self.active and published is not None:
                self._release(RELEASE_CLEAN, published)
            self.latched = False
            return
        if self.latched or published is None:
            return
        reason = REASON_INFEASIBLE
        hit = infeasible_spike(run, self.threshold) if spike else None
        if (
            hit is None
            and control_room is not None
            and self.last_dispatch is not None
            and self.last_dispatch < DISPATCH_CEILING_KWH
        ):
            reason, hit = REASON_CONTROL_ROOM, dict(control_room)
        if hit is None and dispatch_rhs is not None:
            reason, hit = REASON_DISPATCH_RHS, dispatch_rhs
        if hit is None:
            return
        if self.active:
            self.renewed = published
        else:
            self._activate(reason, published, hit)

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
        self.last_dispatch = price
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
        expires = self.expires_at
        return {
            "active": self.active,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "renewed": self.renewed.isoformat() if self.renewed else None,
            "expires_at": expires.isoformat() if expires else None,
            "threshold": round(self.threshold, 4),
            "detail": dict(self.detail),
            "latched": self.latched,
            "last_release": self.last_release,
        }

    def describe(self, run: dict[str, Any] | None) -> str:
        """One log line for a run while the flag holds."""
        if self.reason == REASON_DISPATCH_RHS:
            d = self.detail
            return (
                f"Nimbus #1778: P5MIN run {(run or {}).get('run_datetime')} "
                f"distrusted ({self.reason} since {self.since.isoformat() if self.since else None}): "
                f"constraint {d.get('constraint_id')} at RHS {d.get('forecast_rhs')} MW "
                f"against dispatch's {d.get('dispatch_rhs')} MW (ratio {d.get('ratio')}); "
                f"forward spike prices >= {self.threshold:.2f}/kWh in the scheduling "
                f"prices are replaced until it releases"
            )
        head = _headline(run or {}) or {}
        return (
            f"Nimbus #1778: P5MIN run {(run or {}).get('run_datetime')} "
            f"distrusted ({self.reason} since {self.since.isoformat() if self.since else None}): "
            f"constraint {head.get('constraint_id')} RHS {head.get('rhs')} MW violated by "
            f"{head.get('violation')} MW; forward spike prices >= "
            f"{self.threshold:.2f}/kWh in the scheduling prices are replaced "
            f"until it releases"
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


def replace_spikes(
    values: Any,
    grid_times: list[datetime],
    threshold: float,
    live_price: float | None,
) -> tuple[Any, int, float | None]:
    """(values, replaced, fallback). Periods inside the current 5-minute
    interval keep their price. Later periods at or above `threshold` take
    the live price when it is not a spike, else the current interval's own
    non-spike price, else the median of the series' non-spike prices. With
    no non-spike price anywhere, nothing is replaced."""
    if len(values) == 0 or not grid_times:
        return values, 0, None
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
        return values, 0, None
    out = values.copy()  # a list or a numpy array, kept as it came
    replaced = 0
    for i, (v, t) in enumerate(zip(values, grid_times)):
        if t >= keep_until and v >= threshold:
            out[i] = fallback
            replaced += 1
    return out, replaced, fallback


_LOGGED: set[str] = set()


def guard_schedule_prices(
    import_prices: Any,
    export_prices: Any,
    grid_times: list[datetime],
    threshold_mwh: float | None,
    read_state: Callable[[str], dict[str, Any]],
) -> tuple[Any, Any]:
    """The scheduling import and export prices ($/kWh, the site's own) after
    the guard. Unchanged -- the same objects -- unless a forward period is at
    or above the threshold AND the P5MIN sensor's flag holds; the flag is
    read only then, so a solve with no spike reads nothing extra. A high
    positive feed-in is a spike on the export side exactly as a high import
    price is on the import side. Never raises: a failed read is no flag."""
    threshold = threshold_kwh(threshold_mwh)
    if not grid_times or not any(
        v >= threshold for prices in (import_prices, export_prices) for v in prices
    ):
        return import_prices, export_prices
    try:
        flag = (read_state(P5MIN_ENTITY_ID).get("attributes") or {}).get(ATTRIBUTE)
    except Exception:  # noqa: BLE001 -- no P5MIN sensor, no flag
        return import_prices, export_prices
    if not isinstance(flag, dict) or not holds(flag, grid_times[0]):
        return import_prices, export_prices
    out = []
    for side, prices in (("import", import_prices), ("export", export_prices)):
        values, replaced, fallback = replace_spikes(prices, grid_times, threshold, None)
        out.append(values if replaced else prices)
        key = f"{flag.get('since')}|{side}"
        if replaced and key not in _LOGGED:
            if len(_LOGGED) > 64:
                _LOGGED.clear()
            _LOGGED.add(key)
            _LOGGER.warning(
                "Nimbus #1778: %d forward %s spike period(s) replaced with "
                "%.4f/kWh while the P5MIN false-spike flag holds (%s since %s, "
                "until %s)",
                replaced,
                side,
                fallback,
                flag.get("reason"),
                flag.get("since"),
                flag.get("expires_at"),
            )
    return out[0], out[1]
