"""Coordinator for Nimbus -- owns the trained model, the daily retrain
schedule, and the periodic (cheap, inference-only) forecast-publish cycle.

Two independent timers, deliberately kept separate:
  - Retrain (expensive, once a day, via async_track_time_change at a
    configured local hour): re-fits the model from real recorder history.
  - Update (cheap, every UPDATE_INTERVAL_MINUTES, the normal
    DataUpdateCoordinator tick): re-runs inference with whatever model is
    currently loaded and republishes the forecast. Never retrains.

Every call into scikit-learn or disk I/O goes through an executor
(hass.async_add_executor_job / the recorder's own executor for DB reads) --
never directly on the event loop.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta
import logging
import pickle
from pathlib import Path
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_CURTAILMENT_SENSOR,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_HUMIDITY_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_SCHEDULE_END_HOUR,
    CONF_SCHEDULE_START_HOUR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    DEFAULT_FALLBACK_HUMIDITY_PCT,
    DEFAULT_FALLBACK_TEMPERATURE_C,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
    DOMAIN,
    LAG_LONG_STEPS,
    MIN_TRAINING_POINTS,
    RESAMPLE_MINUTES,
    UPDATE_INTERVAL_MINUTES,
)
from .ml.features import FEATURE_NAMES
from .ml.model import TrainedModel, predict, train_model

_LOGGER = logging.getLogger(__name__)


class NimbusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns one load's model lifecycle and produces its published forecast
    payload. One instance per "load" subentry -- e.g. HWS L1, HWS L3, and
    Pool each get their own coordinator (and own device, own model, own
    forecast sensor) even though they all live under the same Nimbus hub
    config entry.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{subentry.subentry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self.subentry = subentry
        self._trained: TrainedModel | None = None
        self._model_path = Path(
            hass.config.path(".storage", f"nimbus_load_{subentry.subentry_id}.pkl")
        )
        self._unsub_retrain: Any = None
        self._retraining = False

    # -- config accessors -- only the load sensor is per-subentry (the one
    # thing that's genuinely different for each load). Everything else
    # (temperature sensors, horizon, retrain hour, training window) is a
    # *shared* setting, set once on the hub's own "Configure" and read from
    # entry.options -- moved here 2026-08-14 after re-entering the same
    # values on every one of 18 planned loads was real, unnecessary
    # friction. -----------------------------------------------------------

    @property
    def _load_sensor(self) -> str:
        return self.subentry.data[CONF_LOAD_SENSOR]

    @property
    def _temp_sensor(self) -> str | None:
        return self.entry.options.get(CONF_TEMPERATURE_SENSOR)

    @property
    def _temp_forecast_sensor(self) -> str | None:
        return self.entry.options.get(CONF_TEMPERATURE_FORECAST_SENSOR)

    @property
    def _humidity_sensor(self) -> str | None:
        return self.entry.options.get(CONF_HUMIDITY_SENSOR)

    @property
    def _curtailment_sensor(self) -> str | None:
        return self.entry.options.get(CONF_CURTAILMENT_SENSOR)

    # Per-load, unlike everything else above -- a fixed schedule window
    # (if any) is specific to this one load, not shared across the hub.
    # Stored as a plain HH:MM(:SS) time string (what the config flow's
    # TimeSelector actually produces -- a real time picker, not a
    # decimal-hour number box) and converted to the decimal hour
    # ml/features.py's in_schedule comparison expects right here, so
    # nothing downstream of these two properties needs to know the
    # storage format changed.
    @property
    def _schedule_start_hour(self) -> float | None:
        return _parse_time_to_hour(self.subentry.data.get(CONF_SCHEDULE_START_HOUR))

    @property
    def _schedule_end_hour(self) -> float | None:
        return _parse_time_to_hour(self.subentry.data.get(CONF_SCHEDULE_END_HOUR))

    @property
    def _horizon_hours(self) -> int:
        return self.entry.options.get(
            CONF_FORECAST_HORIZON_HOURS, DEFAULT_FORECAST_HORIZON_HOURS
        )

    @property
    def _retrain_hour(self) -> int:
        return self.entry.options.get(CONF_RETRAIN_HOUR_LOCAL, DEFAULT_RETRAIN_HOUR_LOCAL)

    @property
    def _train_days(self) -> int:
        return self.entry.options.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS)

    # -- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load a persisted model if present, then wire up the nightly retrain."""
        self._trained = await self.hass.async_add_executor_job(self._load_model_from_disk)

        self._unsub_retrain = async_track_time_change(
            self.hass,
            self._handle_retrain_trigger,
            hour=self._retrain_hour,
            minute=0,
            second=0,
        )

        if self._trained is None:
            # Nothing on disk yet -- train immediately so the sensor has real
            # data soon after setup, instead of sitting empty for up to 24h
            # waiting for the next scheduled retrain hour.
            await self._async_retrain()

    def async_unload(self) -> None:
        if self._unsub_retrain is not None:
            self._unsub_retrain()
            self._unsub_retrain = None

    async def _handle_retrain_trigger(self, _now: datetime) -> None:
        await self._async_retrain()

    async def _async_retrain(self) -> None:
        if self._retraining:
            _LOGGER.debug("Retrain already in progress, skipping overlapping trigger.")
            return
        self._retraining = True
        try:
            end = dt_util.utcnow()
            start = end - timedelta(days=self._train_days)

            load_events = await self._async_fetch_history(
                self._load_sensor, start, end, convert_power=True
            )
            temp_events = (
                await self._async_fetch_history(self._temp_sensor, start, end)
                if self._temp_sensor
                else []
            )
            humidity_events = (
                await self._async_fetch_history(self._humidity_sensor, start, end)
                if self._humidity_sensor
                else []
            )
            curtailment_events = (
                await self._async_fetch_history(self._curtailment_sensor, start, end, binary=True)
                if self._curtailment_sensor
                else []
            )

            trained = await self.hass.async_add_executor_job(
                _train_model_job, load_events, temp_events, humidity_events, curtailment_events,
                start, end, self._schedule_start_hour, self._schedule_end_hour,
            )
            if trained is not None:
                self._trained = trained
                await self.hass.async_add_executor_job(self._save_model_to_disk, trained)
                await self.async_request_refresh()
        finally:
            self._retraining = False

    # -- data access ----------------------------------------------------------

    async def _async_fetch_history(
        self, entity_id: str, start: datetime, end: datetime, *,
        convert_power: bool = False, binary: bool = False,
    ) -> list[tuple[datetime, float]]:
        """Fetch recorder history for one entity, in-process -- no REST call,
        no token, identical on HAOS/Supervised/Docker. Goes through the
        recorder's own executor (not hass.async_add_executor_job) per Home
        Assistant convention for recorder DB access.

        `convert_power=True` (used for the load sensor, never temperature)
        reads each history point's own recorded `unit_of_measurement` and
        converts it to kW via Home Assistant's own PowerConverter, rather
        than assuming the source is already in kW. Confirmed live
        2026-08-14: a real source sensor can report W while a visually
        similar sibling reports kW -- silently assuming kW on a W-unit
        sensor would train (and later publish) numbers 1000x too small.
        Falls back to treating an unrecognized/missing unit as already-kW
        (logged once) rather than dropping the point entirely.

        `binary=True` (used for the curtailment switch, which reports
        "on"/"off", not a number -- a plain `float(s.state)` would raise
        and silently drop every single point) maps "on" -> 1.0, anything
        else -> 0.0.
        """
        warned_missing_unit = False

        def _fetch() -> list[tuple[datetime, float]]:
            nonlocal warned_missing_unit
            states = get_significant_states(
                self.hass,
                start,
                end,
                [entity_id],
                minimal_response=False,
                significant_changes_only=False,
            ).get(entity_id, [])
            out: list[tuple[datetime, float]] = []
            for s in states:
                if binary:
                    out.append((dt_util.as_local(s.last_changed), 1.0 if s.state == "on" else 0.0))
                    continue
                try:
                    value = float(s.state)
                except (TypeError, ValueError):
                    continue
                if convert_power:
                    unit = s.attributes.get("unit_of_measurement")
                    if unit and unit != UnitOfPower.KILO_WATT:
                        try:
                            value = PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)
                        except Exception:
                            if not warned_missing_unit:
                                _LOGGER.warning(
                                    "%s reported unconvertible unit '%s' -- treating as kW as-is",
                                    entity_id, unit,
                                )
                                warned_missing_unit = True
                out.append((dt_util.as_local(s.last_changed), value))
            return out

        return await get_instance(self.hass).async_add_executor_job(_fetch)

    def _current_humidity(self) -> float:
        """No humidity-forecast integration exists (unlike temperature) to
        source a horizon-length forecast from, so the current reading is
        held constant across the whole forecast horizon -- a reasonable
        approximation for humidity, which typically moves far more slowly
        over a 48h horizon than the diurnal temperature swing does.
        """
        if self._humidity_sensor is None:
            return DEFAULT_FALLBACK_HUMIDITY_PCT
        state = self.hass.states.get(self._humidity_sensor)
        if state is None:
            return DEFAULT_FALLBACK_HUMIDITY_PCT
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return DEFAULT_FALLBACK_HUMIDITY_PCT

    async def _async_fetch_temperature_forecast(self) -> list[tuple[datetime, float]]:
        if self._temp_forecast_sensor is None:
            return []
        state = self.hass.states.get(self._temp_forecast_sensor)
        if state is None:
            return []
        forecast = state.attributes.get("forecast", [])
        out: list[tuple[datetime, float]] = []
        for entry in forecast:
            try:
                ts = dt_util.parse_datetime(entry["datetime"])
                temp = float(entry["temperature"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts is not None:
                out.append((ts, temp))
        out.sort(key=lambda x: x[0])
        return out

    async def _async_fetch_curtailment_forecast(self) -> list[tuple[datetime, float]]:
        """HAEO's own curtailment switch carries a real forward `forecast`
        attribute (confirmed live 2026-08-14: {"time": ..., "value": bool}
        entries) -- HAEO already plans curtailment ahead of time, so this is
        genuinely forward-looking, unlike humidity's held-flat approximation.
        """
        if self._curtailment_sensor is None:
            return []
        state = self.hass.states.get(self._curtailment_sensor)
        if state is None:
            return []
        forecast = state.attributes.get("forecast", [])
        out: list[tuple[datetime, float]] = []
        for entry in forecast:
            try:
                ts = dt_util.parse_datetime(entry["time"])
                value = 1.0 if entry["value"] else 0.0
            except (KeyError, TypeError, ValueError):
                continue
            if ts is not None:
                out.append((ts, value))
        out.sort(key=lambda x: x[0])
        return out

    # -- model persistence ----------------------------------------------------

    def _load_model_from_disk(self) -> TrainedModel | None:
        if not self._model_path.exists():
            return None
        try:
            trained = pickle.loads(self._model_path.read_bytes())
        except Exception:
            _LOGGER.warning("Could not load persisted model, will retrain.", exc_info=True)
            return None
        # A persisted model's feature count is frozen at whatever
        # FEATURE_NAMES looked like the day it was trained. Confirmed live
        # 2026-08-15: loading an incompatible model straight into predict()
        # raises a raw numpy broadcast ValueError deep inside an executor
        # thread, which HA surfaces as an opaque "Config entry not ready
        # yet" retry loop with no obvious fix -- every retry hits the same
        # crash since the stale pickle never gets replaced on its own.
        # Discarding it here instead means a future feature-set change
        # self-heals with one fresh retrain, not a stuck integration.
        if trained.x_mean.shape[0] != len(FEATURE_NAMES):
            _LOGGER.warning(
                "Persisted model for %s has %d feature(s) but the current code "
                "expects %d (trained under an older version) -- discarding and "
                "retraining fresh instead of crashing.",
                self.subentry.subentry_id, trained.x_mean.shape[0], len(FEATURE_NAMES),
            )
            return None
        return trained

    def _save_model_to_disk(self, trained: TrainedModel) -> None:
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._model_path.write_bytes(pickle.dumps(trained))

    # -- coordinator tick (cheap: inference only) ------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if self._trained is None:
            return {"state": None, "forecast": [], "trained_at": None, "training_points": 0}

        now_utc = dt_util.utcnow()
        horizon_end = now_utc + timedelta(hours=self._horizon_hours)

        temp_forecast = await self._async_fetch_temperature_forecast()
        fallback_temp = temp_forecast[0][1] if temp_forecast else DEFAULT_FALLBACK_TEMPERATURE_C
        current_humidity = self._current_humidity()
        curtailment_forecast = await self._async_fetch_curtailment_forecast()

        # Real recent history, not just the model's own state -- this is
        # the lag-feature seed. Fetched with a comfortable multiple of
        # margin past LAG_LONG_STEPS so a real value is always available at
        # the very first forecast step even if the recorder's most recent
        # write is a few minutes stale.
        lag_lookback = timedelta(minutes=RESAMPLE_MINUTES * (LAG_LONG_STEPS + 4))
        recent_load_values = await self._async_fetch_history(
            self._load_sensor, now_utc - lag_lookback, now_utc, convert_power=True
        )

        timestamps: list[datetime] = []
        temps: list[float] = []
        humidities: list[float] = []
        curtailments: list[float] = []
        t = now_utc
        step = timedelta(minutes=RESAMPLE_MINUTES)
        while t <= horizon_end:
            timestamps.append(dt_util.as_local(t))
            temps.append(_nearest_temp(temp_forecast, t, fallback_temp))
            humidities.append(current_humidity)
            curtailments.append(_step_lookup(curtailment_forecast, t, 0.0))
            t += step

        preds = await self.hass.async_add_executor_job(
            predict,
            self._trained,
            timestamps,
            temps,
            humidities,
            recent_load_values,
            RESAMPLE_MINUTES,
            curtailments,
            self._schedule_start_hour,
            self._schedule_end_hour,
        )

        points = [
            {"time": ts.isoformat(), "value": round(v, 3)}
            for ts, v in zip(timestamps, preds, strict=True)
        ]
        current = preds[0] if preds else 0.0

        return {
            "state": round(current, 3),
            "forecast": points,
            "trained_at": self._trained.trained_at.isoformat(),
            "training_points": self._trained.training_points,
        }


def _train_model_job(
    load_events: list[tuple[datetime, float]],
    temp_events: list[tuple[datetime, float]],
    humidity_events: list[tuple[datetime, float]],
    curtailment_events: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    schedule_start_hour: float | None,
    schedule_end_hour: float | None,
) -> TrainedModel | None:
    """Plain function (not a bound method) so it's cleanly picklable/callable
    from hass.async_add_executor_job without capturing `self`.
    """
    return train_model(
        load_events=load_events,
        temp_events=temp_events,
        humidity_events=humidity_events,
        curtailment_events=curtailment_events,
        start=dt_util.as_local(start),
        end=dt_util.as_local(end),
        resample_minutes=RESAMPLE_MINUTES,
        min_training_points=MIN_TRAINING_POINTS,
        schedule_start_hour=schedule_start_hour,
        schedule_end_hour=schedule_end_hour,
    )


def _parse_time_to_hour(value: str | float | int | None) -> float | None:
    """Convert a stored schedule value into a decimal hour, e.g.
    "12:30:00" -> 12.5 -- the form ml/features.py's in_schedule
    comparison expects. Returns None for an unset field.

    Handles two storage shapes: HA's TimeSelector "HH:MM:SS" string
    (current), and a bare decimal-hour float/int (the pre-2026-08-15
    NumberSelector-based flow's own format). Confirmed live: a load
    reconfigured under the old NumberSelector (Pool 1, saved as e.g.
    8.0) still has that raw float sitting in its subentry data --
    calling .split(':') on it unconditionally crashed every coordinator
    refresh with "'float' object has no attribute 'split'". A plain
    number is already the decimal hour, no parsing needed at all.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        parts = value.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        return hour + minute / 60
    except (ValueError, IndexError, AttributeError):
        _LOGGER.warning("Could not parse schedule time value %r -- treating as unset", value)
        return None


def _nearest_temp(
    forecast: list[tuple[datetime, float]], target: datetime, fallback: float
) -> float:
    if not forecast:
        return fallback
    times = [f[0] for f in forecast]
    idx = bisect_right(times, target)
    if idx == 0:
        return forecast[0][1]
    if idx >= len(forecast):
        return forecast[-1][1]
    before, after = forecast[idx - 1], forecast[idx]
    span = (after[0] - before[0]).total_seconds()
    if span <= 0:
        return before[1]
    frac = (target - before[0]).total_seconds() / span
    return before[1] + frac * (after[1] - before[1])


def _step_lookup(
    forecast: list[tuple[datetime, float]], target: datetime, fallback: float
) -> float:
    """Same idea as `_nearest_temp` but a step function, not interpolated --
    correct for a boolean-derived signal like curtailment (on/off), where
    blending toward "half curtailed" between two points would be meaningless.
    Returns whichever entry is most recently at-or-before `target`.
    """
    if not forecast:
        return fallback
    times = [f[0] for f in forecast]
    idx = bisect_right(times, target) - 1
    return forecast[idx][1] if idx >= 0 else forecast[0][1]
