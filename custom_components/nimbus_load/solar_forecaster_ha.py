"""Nimbus's own solar forecaster, running inside Home Assistant (the pure engine is solar_forecaster/).

Started when the hub's Configure -> Solar forecaster switch is on. Arrays are the hub's PV String
subentries (each a per-string or per-MPPT power sensor, with the owner's tilt and direction when
given). Three timers, all work in executors:

- weather, hourly: the newest run of each model once it is published (once a day), and the
  evening-before runs of the history it trains on, a few days per call until complete;
- training, daily at 03:30 local (and as soon as there is enough history): the arrays' recorded
  output (raw history where the recorder still has it, long-term hourly statistics before) against
  the day-ahead weather; the honest score comes with it;
- forecast, every 5 minutes: p10 / p50 / p90 for the next four days, corrected by the last 30
  minutes measured, published on sensor.nimbus_solar_forecast in the generic `forecast` shape every
  Solver reader already understands.

Files live in <config>/nimbus_solar_forecaster/ (weather CSVs, the model, the last report).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import datetime as dt
import json
import logging
import math
import multiprocessing
import pickle
import zoneinfo
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PV_STRING_AZIMUTH,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_TILT,
    CONF_SOLAR_FORECASTER_ENABLED,
    CONF_SOLAR_FORECASTER_HISTORY_DAYS,
    DEFAULT_SOLAR_FORECASTER_ENABLED,
    DEFAULT_SOLAR_FORECASTER_HISTORY_DAYS,
    DOMAIN,
    SOLAR_FORECASTER_MIN_TRAINING_DAYS,
    SUBENTRY_TYPE_PV_STRING,
)
from .coordinator import MAX_SANE_POWER_KW
from .power_units import is_power_unit, power_to_kw
from .solar_forecaster import runner, sources
from .solar_forecaster.model import TIERS, Trained
from .solar_forecaster.site import Array, Site
from .solar_forecaster.store import WeatherStore

# Other solar forecasts already installed, found by integration and by the translation keys of their
# daily totals (never by entity name), so they appear with no configuration. Yardsticks only: shown
# beside Nimbus's own forecast and score on the Solar Lab card, never an input to the forecast. A
# forecaster with one config entry per panel plane (Forecast.Solar, Open-Meteo Solar Forecast) is the
# sum of its entries. {platform: (name, {translation key: days ahead})}
YARDSTICKS: dict[str, tuple[str, dict[str, int]]] = {
    "forecast_solar": (
        "Forecast.Solar",
        {"energy_production_today": 0, "energy_production_tomorrow": 1},
    ),
    "open_meteo_solar_forecast": (
        "Open-Meteo",
        {
            "energy_production_today": 0,
            "energy_production_tomorrow": 1,
            **{f"energy_production_d{n}": n for n in range(2, 8)},
        },
    ),
    "solcast_solar": (
        "Solcast",
        {
            "total_kwh_forecast_today": 0,
            "total_kwh_forecast_tomorrow": 1,
            **{f"total_kwh_forecast_d{n}": n - 1 for n in range(3, 8)},
        },
    ),
}
#: The weather decode (pure-Python CCSDS unpacking) and the nightly training are CPU-heavy. Run in
#: Home Assistant's own process they share one interpreter lock with the Solver and slowed its
#: cycles past their limit on a production install (28 Nov 2026: one cycle 165 s, then skipped
#: ticks). They run in one long-lived worker process instead, at the lowest CPU priority, so the
#: Solver and Home Assistant always come first. False runs them in-process (tests).
USE_PROCESS = True
_POOL: concurrent.futures.ProcessPoolExecutor | None = None


def _pool() -> concurrent.futures.ProcessPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            # spawn, never fork: forking a threaded Home Assistant process is unsafe
            mp_context=multiprocessing.get_context("spawn"),
            initializer=runner.low_priority,
        )
    return _POOL


def shutdown_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None


class NoWorkerProcess(RuntimeError):
    """The low-priority worker process could not run here."""


async def run_heavy(hass: HomeAssistant, fn, *args):
    """`fn(*args)` in the low-priority worker process. If that process cannot run here, the
    forecaster stops with NoWorkerProcess rather than run the work inside Home Assistant, where it
    would slow the Solver. An error raised by `fn` itself is passed on as it is."""
    if not USE_PROCESS:  # tests: in-process, so patched functions apply
        return await hass.async_add_executor_job(fn, *args)
    try:
        return await asyncio.wrap_future(_pool().submit(fn, *args))
    except (
        BrokenProcessPool,
        pickle.PicklingError,
        OSError,
    ) as err:
        shutdown_pool()  # a fresh worker is tried next time
        raise NoWorkerProcess(
            f"its worker process could not run ({err}); it does not run inside Home Assistant, "
            "to keep the Solver fast"
        ) from err


#: The evening-before value: a yardstick's "tomorrow" total as it stood at this local hour the day
#: before (by then each has taken in the evening's weather, as Nimbus's own day-ahead has).
YARDSTICK_HOUR = 21


def find_yardsticks(hass: HomeAssistant) -> dict[str, dict[int, list[str]]]:
    """{name: {days ahead: [entity ids]}} of the other solar forecasts installed (enabled only)."""
    from homeassistant.helpers import entity_registry as er

    found: dict[str, dict[int, list[str]]] = {}
    for e in er.async_get(hass).entities.values():
        spec = YARDSTICKS.get(e.platform)
        if spec is None or e.disabled_by is not None:
            continue
        ahead = spec[1].get(e.translation_key or "")
        if ahead is not None:
            found.setdefault(spec[0], {}).setdefault(ahead, []).append(e.entity_id)
    return {
        name: {k: sorted(v) for k, v in sorted(days.items())}
        for name, days in sorted(found.items())
    }


def value_at(points: list[tuple[float, float]], when: float) -> float | None:
    """The last finite value at or before `when` (UTC seconds), from (time, value) points."""
    before = [(ts, v) for ts, v in points if ts <= when and math.isfinite(v)]
    return max(before)[1] if before else None


_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATED = "nimbus_solar_forecast_updated_{}"
DIR_NAME = "nimbus_solar_forecaster"
RAW_HISTORY_DAYS = (
    10  # the recorder's default keep; older days come from long-term statistics
)
FAILING_REPAIR_AFTER = dt.timedelta(hours=26)
ISSUE_NO_ARRAYS = "solar_forecaster_no_arrays"
ISSUE_WEATHER_FAILING = "solar_forecaster_weather_failing"

_RUNTIMES: dict[str, SolarForecasterRuntime] = {}


def enabled(options: dict[str, Any]) -> bool:
    return bool(
        options.get(CONF_SOLAR_FORECASTER_ENABLED, DEFAULT_SOLAR_FORECASTER_ENABLED)
    )


def runtime_for(entry_id: str) -> SolarForecasterRuntime | None:
    return _RUNTIMES.get(entry_id)


@dataclass
class Status:
    """What the sensor publishes about the forecaster itself (beside the forecast)."""

    state: str = (
        "starting"  # starting | fetching_history | learning | ready | no_arrays | error
    )
    detail: str = ""
    history_days: int = 0
    weather_runs: dict[str, str] = field(default_factory=dict)
    weather_failing_since: dict[str, str] = field(default_factory=dict)
    trained_at: str | None = None
    score: dict[str, Any] = field(default_factory=dict)
    arrays: list[dict[str, Any]] = field(default_factory=list)


class SolarForecasterRuntime:
    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.dir = Path(hass.config.path(DIR_NAME))
        self.store = WeatherStore(self.dir / "weather")
        self.site = Site(
            lat=float(hass.config.latitude),
            lon=float(hass.config.longitude),
            altitude_m=float(hass.config.elevation or 0),
        )
        self.tz = zoneinfo.ZoneInfo(hass.config.time_zone)
        self.history_days = int(
            entry.options.get(
                CONF_SOLAR_FORECASTER_HISTORY_DAYS,
                DEFAULT_SOLAR_FORECASTER_HISTORY_DAYS,
            )
        )
        self.arrays = self._arrays_from_subentries()
        self.trained: Trained | None = None
        self.forecast: runner.Forecast | None = None
        self.today: dict[str, Any] = {}
        self.measured_today: list[dict[str, Any]] = []
        self.made_at: str | None = None
        self.yardsticks: dict[str, dict[str, float]] = {}  # {name: {date: kWh}}
        # {name: {days ahead: [entity ids]}}: the card reads their live totals for the days table
        self.yardstick_entities: dict[str, dict[str, list[str]]] = {}
        self.status = Status(arrays=[self._array_view(a) for a in self.arrays])
        self._unsubs: list = []
        self._busy = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # --- arrays -------------------------------------------------------------------------------
    def _arrays_from_subentries(self) -> list[Array]:
        out = []
        for sub in self.entry.subentries.values():
            if sub.subentry_type != SUBENTRY_TYPE_PV_STRING:
                continue
            entity = sub.data.get(CONF_PV_STRING_ENTITY)
            if not entity:
                continue
            tilt, azim = (
                sub.data.get(CONF_PV_STRING_TILT),
                sub.data.get(CONF_PV_STRING_AZIMUTH),
            )
            a = Array(
                key=entity,
                tilt=None if tilt in (None, "") else float(tilt),
                azimuth=None if azim in (None, "") else float(azim),
            )
            a.learned = {
                "label": sub.data.get(CONF_PV_STRING_LABEL) or sub.title,
                "owner_set": a.owner_set,
            }
            out.append(a)
        return out

    @staticmethod
    def _array_view(a: Array) -> dict[str, Any]:
        return {
            "entity_id": a.key,
            "label": a.learned.get("label"),
            "active": a.active,
            "kwp": a.kwp,
            "tilt": a.tilt,
            "azimuth": a.azimuth,
            "orientation": "set by you"
            if a.learned.get("owner_set")
            else ("learned" if a.kwp else "not learned yet"),
            "scattered_light_gain": a.diffuse_gain,
        }

    # --- lifecycle ----------------------------------------------------------------------------
    async def async_start(self) -> None:
        if not self.arrays:
            self.status.state, self.status.detail = (
                "no_arrays",
                "add a PV String for each array",
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_NO_ARRAYS,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NO_ARRAYS,
            )
            self._publish()
            return
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_ARRAYS)
        await self.hass.async_add_executor_job(self._load_saved)
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._tick_weather, dt.timedelta(hours=1)
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._tick_forecast, dt.timedelta(minutes=5)
            )
        )
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._tick_train, hour=3, minute=30, second=0
            )
        )
        self._task = self.hass.async_create_background_task(
            self._async_bootstrap(), "nimbus solar forecaster start"
        )

    @callback
    def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _load_saved(self) -> None:
        path = self.dir / "model.pkl"
        if not path.exists():
            return
        try:
            saved = pickle.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 -- an unreadable model is retrained, never fatal
            _LOGGER.warning(
                "Nimbus solar forecaster: the saved model could not be read; it will retrain"
            )
            return
        if saved.get("arrays_key") != self._arrays_key():
            return  # the PV strings changed since: retrain
        self.trained = saved["trained"]
        self.site.linke = saved.get("linke", self.site.linke)
        for a, learned in zip(self.arrays, saved.get("arrays", []), strict=False):
            a.kwp, a.diffuse_gain, a.active = (
                learned["kwp"],
                learned["diffuse_gain"],
                learned["active"],
            )
            if a.tilt is None:
                a.tilt, a.azimuth = learned["tilt"], learned["azimuth"]
            a.learned.update(
                {
                    k: v
                    for k, v in learned.get("learned", {}).items()
                    if k not in ("label", "owner_set")
                }
            )
        self.status.trained_at = saved.get("trained_at")
        self.status.score = saved.get("score", {})
        self.status.arrays = [self._array_view(a) for a in self.arrays]

    def _arrays_key(self) -> str:
        """The PV strings as configured (entity, and the owner's tilt and direction): a change retrains."""
        return json.dumps(
            [
                (
                    a.key,
                    a.tilt if a.learned.get("owner_set") else None,
                    a.azimuth if a.learned.get("owner_set") else None,
                )
                for a in self.arrays
            ]
        )

    def _save(self, trained: Trained) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "arrays_key": self._arrays_key(),
            "trained": trained,
            "linke": self.site.linke,
            "arrays": [
                {
                    "kwp": a.kwp,
                    "diffuse_gain": a.diffuse_gain,
                    "active": a.active,
                    "tilt": a.tilt,
                    "azimuth": a.azimuth,
                    "learned": a.learned,
                }
                for a in self.arrays
            ],
            "trained_at": self.status.trained_at,
            "score": self.status.score,
        }
        tmp = self.dir / "model.pkl.tmp"
        tmp.write_bytes(pickle.dumps(payload))
        tmp.replace(self.dir / "model.pkl")
        (self.dir / "report.json").write_text(
            json.dumps(trained.report, default=float, indent=1)
        )

    # --- the three jobs -----------------------------------------------------------------------
    async def _async_bootstrap(self) -> None:
        """At start: forecast with the saved model at once, then fetch until the history is in,
        then train if there is no current model."""
        await self._async_forecast()
        for _ in range(self.history_days // 3 + 3):
            more = await self._async_weather()
            if not more:
                break
            await asyncio.sleep(20)
        if (
            self.trained is None
            or self._model_age() > dt.timedelta(days=1)
            # saved by an older release without a tier this one has (days 3-5's GFS-only tier):
            # learn it now rather than leave those days empty until the night's training
            or set(TIERS) - set(self.trained.tiers)
        ):
            await self._async_train()
        await self._async_yardsticks()
        await self._async_forecast()

    async def _async_yardsticks(self) -> None:
        """Each scored day's evening-before "tomorrow" total of every other solar forecast
        installed, from the recorder (a forecast the recorder does not keep stays empty)."""
        rows = self.trained.report.get("rows", []) if self.trained else []
        everything = find_yardsticks(self.hass)
        self.yardstick_entities = {
            name: {str(k): v for k, v in days.items()}
            for name, days in everything.items()
        }
        found = {name: days[1] for name, days in everything.items() if 1 in days}
        if not rows or not found:
            self.yardsticks = {name: {} for name in everything}
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states

        dates = [dt.date.fromisoformat(r["date"]) for r in rows]
        cut = {
            d: dt.datetime.combine(
                d - dt.timedelta(days=1), dt.time(YARDSTICK_HOUR), self.tz
            ).timestamp()
            for d in dates
        }
        start = dt_util.utc_from_timestamp(min(cut.values()) - 2 * 86400)
        end = dt_util.utc_from_timestamp(max(cut.values()) + 60)
        ids = [i for v in found.values() for i in v]

        def _read() -> dict[str, list[tuple[float, float]]]:
            states = get_significant_states(
                self.hass,
                start,
                end,
                ids,
                minimal_response=True,
                significant_changes_only=False,
            )
            out: dict[str, list[tuple[float, float]]] = {}
            for eid in ids:
                unit = (self.hass.states.get(eid) or None) and self.hass.states.get(
                    eid
                ).attributes.get("unit_of_measurement")
                scale = 0.001 if unit == "Wh" else 1.0
                pts = []
                for s in states.get(eid, []):
                    raw = s["state"] if isinstance(s, dict) else s.state
                    when = (
                        dt_util.parse_datetime(s["last_changed"]).timestamp()
                        if isinstance(s, dict)
                        else s.last_changed.timestamp()
                    )
                    try:
                        pts.append((when, float(raw) * scale))
                    except (TypeError, ValueError):
                        continue
                out[eid] = pts
            return out

        try:
            hist = await get_instance(self.hass).async_add_executor_job(_read)
        except Exception as err:  # noqa: BLE001 -- yardsticks are optional; the card shows dashes
            _LOGGER.debug("Nimbus solar forecaster: yardsticks not read (%s)", err)
            return
        out: dict[str, dict[str, float]] = {}
        for name, eids in found.items():
            per_day: dict[str, float] = {}
            for d in dates:
                vals = [value_at(hist.get(e, []), cut[d]) for e in eids]
                if vals and all(v is not None for v in vals):
                    per_day[d.isoformat()] = round(sum(vals), 1)
            out[name] = per_day
        self.yardsticks = out

    async def _tick_weather(self, _now) -> None:
        await self._async_weather()

    async def _tick_forecast(self, _now) -> None:
        await self._async_forecast()

    async def _tick_train(self, _now) -> None:
        await self._async_train()
        await self._async_yardsticks()  # also picks up a forecaster installed since
        await self._async_forecast()

    def _model_age(self) -> dt.timedelta:
        if not self.status.trained_at:
            return dt.timedelta(days=365)
        return dt_util.utcnow() - dt.datetime.fromisoformat(self.status.trained_at)

    async def _async_weather(self) -> bool:
        """One weather pass. True while history is still being filled in."""
        async with self._busy:
            if (
                self.status.state in ("starting", "fetching_history")
                and self.trained is None
            ):
                self.status.state = "fetching_history"
            now = dt_util.utcnow()
            try:
                rep = await run_heavy(
                    self.hass,
                    runner.fetch_weather,
                    self.store,
                    self.site.lat,
                    self.site.lon,
                    now,
                    self.tz,
                    self.history_days,
                )
            except Exception as err:  # noqa: BLE001 -- the network is allowed to fail; the next pass retries
                _LOGGER.warning(
                    "Nimbus solar forecaster: weather fetch failed (%s); retrying next hour",
                    err,
                )
                rep = runner.FetchReport(failed={s: str(err) for s in sources.SPECS})
            for s in sources.SPECS:
                runs = await self.hass.async_add_executor_job(self.store.runs, s)
                if runs:
                    self.status.weather_runs[s] = f"{runs[-1]:%Y-%m-%d %H}Z"
                if s in rep.failed:
                    self.status.weather_failing_since.setdefault(s, now.isoformat())
                else:
                    self.status.weather_failing_since.pop(s, None)
            failing = [
                s
                for s, since in self.status.weather_failing_since.items()
                if now - dt.datetime.fromisoformat(since) > FAILING_REPAIR_AFTER
            ]
            if len(failing) == len(sources.SPECS):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_WEATHER_FAILING,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_WEATHER_FAILING,
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_WEATHER_FAILING)
            self.status.history_days = await self.hass.async_add_executor_job(
                self._history_days_in_store
            )
            self._publish()
            return rep.backfilled_days > 0

    def _history_days_in_store(self) -> int:
        today = dt_util.now(self.tz).date()
        return sum(
            1
            for back in range(1, self.history_days + 1)
            if all(
                self.store.has(
                    s, sources.evening_before_run(s, today - dt.timedelta(days=back))
                )
                for s in sources.SPECS
            )
        )

    async def _async_measured(
        self, start: dt.datetime, end: dt.datetime
    ) -> tuple[np.ndarray, dict]:
        """5-minute means (kW) per array on one grid: raw history for the recorder's last days,
        long-term hourly statistics spread by the clear-sky shape before that."""
        # recorder imports here, not at module level: the module stays importable where the recorder
        # is not (the stub test harness), and only a running forecaster needs them
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        t0, t1 = start.timestamp(), end.timestamp()
        raw_from = max(t0, (end - dt.timedelta(days=RAW_HISTORY_DAYS)).timestamp())
        grid = np.arange(np.floor(t0 / 300) * 300, t1, 300.0)
        ids = [a.key for a in self.arrays]

        def _raw() -> dict[str, list[tuple[float, float]]]:
            states = get_significant_states(
                self.hass,
                dt_util.utc_from_timestamp(raw_from),
                end,
                ids,
                minimal_response=False,
                significant_changes_only=False,
            )
            out: dict[str, list[tuple[float, float]]] = {}
            for eid in ids:
                pts = []
                for s in states.get(eid, []):
                    try:
                        v = float(s.state)
                    except (TypeError, ValueError):
                        pts.append((s.last_changed.timestamp(), math.nan))
                        continue
                    unit = s.attributes.get("unit_of_measurement")
                    if unit and is_power_unit(unit):
                        v = power_to_kw(v, unit)
                    if not math.isfinite(v) or abs(v) > MAX_SANE_POWER_KW:
                        continue
                    pts.append((s.last_changed.timestamp(), max(v, 0.0)))
                out[eid] = pts
            return out

        def _lts() -> dict[str, list[tuple[float, float]]]:
            if raw_from <= t0:
                return {}
            rows = statistics_during_period(
                self.hass,
                start,
                dt_util.utc_from_timestamp(raw_from),
                set(ids),
                "hour",
                None,
                {"mean"},
            )
            out: dict[str, list[tuple[float, float]]] = {}
            for eid, rs in rows.items():
                unit = (
                    self.hass.states.get(eid).attributes.get("unit_of_measurement")
                    if self.hass.states.get(eid)
                    else None
                )
                pts = []
                for r in rs:
                    m = r.get("mean")
                    if m is None or not math.isfinite(float(m)):
                        continue
                    v = (
                        power_to_kw(float(m), unit)
                        if unit and is_power_unit(unit)
                        else float(m)
                    )
                    st = r.get("start")
                    ts = (
                        st
                        if isinstance(st, int | float)
                        else (st.timestamp() if st is not None else None)
                    )
                    if ts is not None and abs(v) <= MAX_SANE_POWER_KW:
                        pts.append((float(ts), max(v, 0.0)))
                out[eid] = pts
            return out

        rec = get_instance(self.hass)
        raw = await rec.async_add_executor_job(_raw)
        lts = await rec.async_add_executor_job(_lts)

        def _assemble() -> dict:
            out = {}
            for a in self.arrays:
                _, r5 = runner.measured_5min(raw.get(a.key, []), grid[0], t1)
                h5 = runner.hourly_to_5min(self.site, a, lts.get(a.key, []), grid)
                out[a.key] = np.where(np.isnan(r5), h5, r5)
            return out

        return grid, await self.hass.async_add_executor_job(_assemble)

    async def _async_train(self) -> None:
        async with self._busy:
            if self.status.history_days < SOLAR_FORECASTER_MIN_TRAINING_DAYS:
                self.status.state = "learning"
                self.status.detail = f"{self.status.history_days} of {SOLAR_FORECASTER_MIN_TRAINING_DAYS} days of weather history so far"
                self._publish()
                return
            end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            start = end - dt.timedelta(days=self.history_days)
            # learn on copies: the 5-minute forecast keeps using the current ones meanwhile, and
            # a failed night leaves them untouched
            site_new, arrays_new = copy.deepcopy(self.site), copy.deepcopy(self.arrays)
            for a in arrays_new:  # a direction it learned is learned again each night
                if not a.learned.get("owner_set"):
                    a.tilt = a.azimuth = None
            try:
                grid, measured = await self._async_measured(start, end)
                trained, site_new, arrays_new = await run_heavy(
                    self.hass,
                    runner.train_job,
                    self.store,
                    site_new,
                    arrays_new,
                    grid,
                    measured,
                    self.tz,
                )
            except Exception as err:  # noqa: BLE001 -- keep the last model; say why on the sensor
                _LOGGER.warning(
                    "Nimbus solar forecaster: training failed (%s); keeping the last model",
                    err,
                )
                self.status.state = "error" if self.trained is None else "ready"
                self.status.detail = f"training failed: {err}"
                self._publish()
                return
            self.site, self.arrays, self.trained = site_new, arrays_new, trained
            self.status.trained_at = dt_util.utcnow().isoformat()
            rep = trained.report
            self.status.score = {
                k: rep.get(k)
                for k in (
                    "scored_days",
                    "daily_error_pct",
                    "p10_p90_cover",
                    "band_scale",
                )
            }
            self.status.arrays = [self._array_view(a) for a in self.arrays]
            await self.hass.async_add_executor_job(self._save, trained)
            self.status.state, self.status.detail = "ready", ""
            self._publish()

    async def _async_forecast(self) -> None:
        if self.trained is None:
            self._publish()
            return
        now = dt_util.utcnow()
        midnight = dt_util.start_of_local_day(dt_util.now(self.tz))
        try:
            # since local midnight: today's output so far for the card, and its last 30
            # minutes for the live correction
            t, measured = await self._async_measured(
                min(now - dt.timedelta(minutes=40), midnight), now
            )
            keys = [a.key for a in self.arrays if a.active]
            recent = None
            if keys:
                stack = np.array([measured[k] for k in keys])
                total = np.where(
                    np.isnan(stack).all(axis=0), np.nan, np.nansum(stack, axis=0)
                )
                if not np.isnan(total).all():
                    recent = (t, total)
            fc = await self.hass.async_add_executor_job(
                runner.forecast,
                self.trained,
                self.store,
                self.site,
                self.arrays,
                now,
                self.tz,
                recent,
            )
        except Exception as err:  # noqa: BLE001 -- the last forecast stays published
            _LOGGER.warning("Nimbus solar forecaster: forecast failed (%s)", err)
            return
        self.forecast = fc
        self.made_at = now.isoformat()
        self._today(fc, recent, midnight.timestamp())
        if self.status.state not in ("error",):
            self.status.state = "ready" if fc.rows else "learning"
        uncovered = "no weather covers this hour yet: the power now is unknown"
        if fc.now_kw is None and fc.rows:
            self.status.detail = uncovered
        elif self.status.detail == uncovered:
            self.status.detail = ""
        self._publish()

    def _today(self, fc, recent, t_midnight: float) -> None:
        """Today on the card: what the panels made since midnight (15-minute means) and the
        day's total so far plus the forecast for the rest of it."""
        self.measured_today, made = [], None
        if recent is not None:
            t, kw = recent
            k = t >= t_midnight
            if k.any():
                made = float(np.nansum(kw[k]) / 12)
                q = np.floor(t[k] / 900) * 900
                for slot in np.unique(q):
                    v = kw[k][q == slot]
                    if np.isfinite(v).any():
                        self.measured_today.append(
                            {
                                "time": dt_util.utc_from_timestamp(slot).isoformat(),
                                "value": round(float(np.nanmean(v)), 3),
                            }
                        )
        rest = next((d for d in fc.days if d.get("partial")), None)
        if rest is None:
            self.today = {}
            return
        base = made or 0.0
        self.today = {
            "date": rest["date"],
            "made_so_far_kwh": None if made is None else round(made, 1),
            "rest_kwh": rest["p50"],
            "p10": round(base + rest["p10"], 1),
            "p50": round(base + rest["p50"], 1),
            "p90": round(base + rest["p90"], 1),
            "clear": rest["clear"],
            "model": rest["model"],
        }

    @callback
    def _publish(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED.format(self.entry.entry_id))

    # --- what the sensor shows ----------------------------------------------------------------
    def attributes(self) -> dict[str, Any]:
        fc = self.forecast
        return {
            "forecast": fc.rows if fc else [],
            "days": [d for d in fc.days if not d.get("partial")] if fc else [],
            "made_at": self.made_at,
            "haze": round(
                float(self.site.linke), 2
            ),  # learned Linke turbidity of the sky
            "today": self.today,
            "measured_today": self.measured_today,
            # its own daily score: each scored day's forecast (made from the evening-before
            # weather, on weeks it was not trained on) against what the panels made
            "scoreboard": [
                {
                    **r,
                    "others": {
                        name: days.get(r["date"])
                        for name, days in self.yardsticks.items()
                    },
                }
                for r in (self.trained.report.get("rows", []) if self.trained else [])
            ],
            # the other solar forecasts installed (yardsticks only, never inputs): their evening-
            # before totals are in the scoreboard rows, their live daily totals are these entities
            "yardsticks": self.yardstick_entities,
            "model": fc.tier if fc else None,
            "live_correction": fc.live_ratio if fc else None,
            "status": self.status.state,
            "status_detail": self.status.detail,
            "history_days": self.status.history_days,
            "trained_at": self.status.trained_at,
            "score": self.status.score,
            "arrays": self.status.arrays,
            "weather_runs": self.status.weather_runs,
            "weather_failing_since": self.status.weather_failing_since,
            "attribution": "; ".join(sp.attribution for sp in sources.SPECS.values()),
        }

    def state_kw(self) -> float | None:
        return None if self.forecast is None else self.forecast.now_kw


async def async_start(hass: HomeAssistant, entry) -> None:
    """Start the forecaster for this hub if it is switched on (and stop an old one)."""
    async_stop(entry.entry_id)
    if not enabled(dict(entry.options)):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_NO_ARRAYS)
        ir.async_delete_issue(hass, DOMAIN, ISSUE_WEATHER_FAILING)
        return
    rt = SolarForecasterRuntime(hass, entry)
    _RUNTIMES[entry.entry_id] = rt
    await rt.async_start()


@callback
def async_stop(entry_id: str) -> None:
    rt = _RUNTIMES.pop(entry_id, None)
    if rt is not None:
        rt.async_stop()
    if not _RUNTIMES:
        shutdown_pool()  # the worker process goes with the last forecaster


def status_dict(entry_id: str) -> dict[str, Any] | None:
    rt = _RUNTIMES.get(entry_id)
    return None if rt is None else asdict(rt.status)
