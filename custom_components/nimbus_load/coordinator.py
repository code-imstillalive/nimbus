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
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FORECAST_HORIZON_HOURS,
    CONF_LOAD_SENSOR,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    DEFAULT_FALLBACK_TEMPERATURE_C,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
    DOMAIN,
    MIN_TRAINING_POINTS,
    RESAMPLE_MINUTES,
    UPDATE_INTERVAL_MINUTES,
)
from .ml.model import TrainedModel, predict, train_model

_LOGGER = logging.getLogger(__name__)


class NimbusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the model lifecycle and produces the published forecast payload."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self._trained: TrainedModel | None = None
        self._model_path = Path(
            hass.config.path(".storage", f"nimbus_load_{entry.entry_id}.pkl")
        )
        self._unsub_retrain: Any = None
        self._retraining = False

    # -- config accessors -------------------------------------------------

    @property
    def _load_sensor(self) -> str:
        return self.entry.data[CONF_LOAD_SENSOR]

    @property
    def _temp_sensor(self) -> str | None:
        return self.entry.data.get(CONF_TEMPERATURE_SENSOR)

    @property
    def _temp_forecast_sensor(self) -> str | None:
        return self.entry.data.get(CONF_TEMPERATURE_FORECAST_SENSOR)

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

            load_events = await self._async_fetch_history(self._load_sensor, start, end)
            temp_events = (
                await self._async_fetch_history(self._temp_sensor, start, end)
                if self._temp_sensor
                else []
            )

            trained = await self.hass.async_add_executor_job(
                _train_model_job, load_events, temp_events, start, end
            )
            if trained is not None:
                self._trained = trained
                await self.hass.async_add_executor_job(self._save_model_to_disk, trained)
                await self.async_request_refresh()
        finally:
            self._retraining = False

    # -- data access ----------------------------------------------------------

    async def _async_fetch_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Fetch recorder history for one entity, in-process -- no REST call,
        no token, identical on HAOS/Supervised/Docker. Goes through the
        recorder's own executor (not hass.async_add_executor_job) per Home
        Assistant convention for recorder DB access.
        """

        def _fetch() -> list[tuple[datetime, float]]:
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
                try:
                    value = float(s.state)
                except (TypeError, ValueError):
                    continue
                out.append((dt_util.as_local(s.last_changed), value))
            return out

        return await get_instance(self.hass).async_add_executor_job(_fetch)

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

    # -- model persistence ----------------------------------------------------

    def _load_model_from_disk(self) -> TrainedModel | None:
        if not self._model_path.exists():
            return None
        try:
            return pickle.loads(self._model_path.read_bytes())
        except Exception:
            _LOGGER.warning("Could not load persisted model, will retrain.", exc_info=True)
            return None

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

        timestamps: list[datetime] = []
        temps: list[float] = []
        t = now_utc
        step = timedelta(minutes=RESAMPLE_MINUTES)
        while t <= horizon_end:
            timestamps.append(dt_util.as_local(t))
            temps.append(_nearest_temp(temp_forecast, t, fallback_temp))
            t += step

        preds = await self.hass.async_add_executor_job(
            predict, self._trained, timestamps, temps
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
    start: datetime,
    end: datetime,
) -> TrainedModel | None:
    """Plain function (not a bound method) so it's cleanly picklable/callable
    from hass.async_add_executor_job without capturing `self`.
    """
    return train_model(
        load_events=load_events,
        temp_events=temp_events,
        start=dt_util.as_local(start),
        end=dt_util.as_local(end),
        resample_minutes=RESAMPLE_MINUTES,
        min_training_points=MIN_TRAINING_POINTS,
    )


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
