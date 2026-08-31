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

import json
import logging
import pickle
from bisect import bisect_right
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .anomaly import ResidualDriftStatus, detect_residual_drift, residual_drift_status
from .const import (
    CONF_BATTERY_SENSOR,
    CONF_CURTAILMENT_SENSOR,
    CONF_EXPECTED_LOAD_KW,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_GRID_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_HYBRID_RECENT_DAYS,
    CONF_LOAD_SENSOR,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_SCHEDULE_END_HOUR,
    CONF_SCHEDULE_START_HOUR,
    CONF_SOLAR_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    CONF_TRAINING_SOURCE,
    DEFAULT_FALLBACK_HUMIDITY_PCT,
    DEFAULT_FALLBACK_TEMPERATURE_C,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_HYBRID_RECENT_DAYS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
    DEFAULT_TRAINING_SOURCE,
    DOMAIN,
    LAG_LONG_STEPS,
    MIN_TRAINING_POINTS,
    RESAMPLE_MINUTES,
    SUBENTRY_TYPE_SIGNAL,
    TRAINING_SOURCE_HYBRID,
    TRAINING_SOURCE_LTS,
    TRAINING_SOURCE_RECORDER,
    UPDATE_INTERVAL_MINUTES,
)
from .ml.features import FEATURE_NAMES
from .ml.model import (
    MAX_RESIDUALS_STORED,
    PredictionResult,
    TrainedModel,
    calibrated_band,
    predict,
    train_model,
)

_LOGGER = logging.getLogger(__name__)

# Idempotent cold-start-retrain task tracking, module-level (survives across
# a re-entrant async_setup_entry() call the same way __init__.py's own
# _solver_timer_unsub already does for the periodic-solve timer -- see that
# dict's own comment for the full "HA abandons and retries a slow
# async_setup_entry()" story, nimbus repo issue #211).
#
# PR #210 backgrounded this cold-start retrain via a bare
# hass.async_create_task(self._async_retrain()) specifically to stop it
# blocking hub setup -- but unlike the periodic-solve timer #213 later
# fixed the SAME way, that task was never tracked or cancelled anywhere.
# self._retraining (an instance attribute) only guards a SINGLE coordinator
# object against being told to retrain twice concurrently -- it does
# nothing for a genuinely SECOND, independent NimbusCoordinator object for
# the SAME subentry_id, which is exactly what a re-entrant
# async_setup_entry() call produces (a fresh `NimbusCoordinator(hass,
# entry, subentry)` per subentry, every time that loop runs -- see
# __init__.py's own async_setup_entry()). Two such coordinators would each
# independently kick off their own untracked, uncancelled retrain against
# the same subentry, each eventually calling _save_model_to_disk()/writing
# their own forecast sensor -- a real, live-plausible contributor to the
# "no longer has a state class" repair recurring on restarts/reloads, the
# same underlying mechanism #210/#213 already fixed for two other call
# sites in this exact class of bug.
_retrain_tasks: dict[str, Any] = {}

# Real, confirmed live 2026-08-17 -- see _async_fetch_recorder_history()'s
# own comment at the point this is used for the full incident. A generous
# physical sanity ceiling on any convert_power=True history point:
# comfortably above anything a real household's own load/battery/grid/
# solar sensor could ever legitimately report (this household's own
# documented real ceiling is ~50kW for Grid/Battery, ~17kW for Solar),
# while safely far below the magnitude a unit-conversion or integer-
# overflow-class sensor glitch produces (these land in the thousands to
# millions, never a plausible near-miss on a real value that this
# threshold could wrongly reject).
MAX_SANE_POWER_KW = 1000.0

# runtime-data (Quality Scale, Bronze): entry.runtime_data replaces the old
# hass.data[DOMAIN][entry.entry_id] pattern. Defined HERE, not in
# __init__.py -- __init__.py already imports FROM sensor.py
# (object_id_from_source), and sensor.py/diagnostics.py both already import
# NimbusCoordinator from THIS module, so this is the one place every
# consumer can import the type alias from without creating an import
# cycle. Forward-references NimbusCoordinator (defined a few lines below,
# not yet at this point in the file) -- safe because PEP 695 `type`
# statements are lazily evaluated on first access, not at definition time,
# by which point the whole module has finished executing.
type NimbusConfigEntry = ConfigEntry[dict[str, NimbusCoordinator]]


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

        # Confidence-band calibration state (see ml/model.py's own
        # calibrated_band() for the actual math). Deliberately persisted
        # SEPARATELY from the trained model, in a plain JSON file, not
        # bundled into TrainedModel -- a retrain replaces self._trained
        # wholesale, but the calibration buffer should survive across
        # retrains (it's tracking how far off PUBLISHED forecasts have
        # been from reality, which stays meaningful regardless of which
        # model produced them).
        self._residual_path = Path(
            hass.config.path(
                ".storage", f"nimbus_load_{subentry.subentry_id}_residuals.json"
            )
        )
        self._residuals: list[float] = []
        # Alert-fatigue guardrail for the anomaly layer's residual-drift
        # check (2026-08-25) -- log once when drift STARTS, not every
        # single tick while it remains ongoing. Reset to False the
        # moment the drift clears, so a genuinely new/repeat episode
        # still logs again rather than being silenced forever by one
        # earlier warning.
        self._residual_drift_flagged = False
        # Always-descriptive telemetry (2026-08-25, nimbus issue #187,
        # Mark Purcell's real-install ask: "a positive 'I'm watching...'
        # telemetry field"). Unlike the flag above (which only tracks
        # whether a WARNING is currently active), this is read by
        # NimbusHealthReportSensor every cycle regardless of whether
        # anything has ever fired -- see _check_residual_drift()'s own
        # comment for why "watching: False" during cold-start is itself
        # a real, honest answer, not a placeholder.
        self._residual_drift_status: ResidualDriftStatus = ResidualDriftStatus(
            watching=False, sample_count=0
        )
        # In-memory only, not persisted -- (timestamp, predicted value)
        # for the nearest-term point of the LAST published forecast, so
        # the NEXT update cycle can compare it against what actually
        # happened. Lost on restart; self-heals within one cycle (~15
        # min), not worth the complexity of persisting a timestamp
        # that's stale the moment HA restarts anyway.
        self._last_step_prediction: tuple[datetime, float] | None = None
        # #269 (Mark Purcell, real repro, 2026-08-28, direct follow-up to
        # #123): a transient weather.get_forecasts failure -- most
        # commonly the HA-restart startup-window race between this
        # coordinator's first tick and the weather integration's first
        # successful fetch -- used to silently degrade that cycle's
        # training input to zero temperature signal, with no recovery
        # path, plus the one-shot warning below made a later CHRONIC
        # failure invisible after its first occurrence. Two independent
        # mitigations, both from Mark's own real proposal:
        #
        # Mitigation A: cache the last real, non-empty forecast on the
        # instance and fall back to a trimmed slice of it on a failed/
        # empty fetch, instead of training on zero signal. Keyed by
        # entity_id too -- a mid-session reconfigure to a DIFFERENT
        # temperature_forecast_sensor must never leak the old sensor's
        # stale cached values onto the new one.
        self._temp_forecast_cache: list[tuple[datetime, float]] = []
        self._temp_forecast_cache_entity: str | None = None
        # Mitigation B: replaces the old one-shot _temp_forecast_empty_
        # warned flag with a state-change tracker -- warns on every
        # success->failure transition (not just the first ever) and logs
        # an INFO on every failure->success recovery, so a chronic
        # problem starting DAYS after the first tick is still loud, and
        # a household triaging a report can tell "did it recover on its
        # own." None = never fetched yet (no state change to log on the
        # very first tick either way).
        self._last_temp_forecast_ok: bool | None = None

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

    # Real measured power sensors only -- never an optimizer's own plan/
    # forecast entity, see this repo's own CLAUDE.md PRIME DIRECTIVE.
    #
    # All three return None (fully disabled) for ANY "power signal"
    # subentry, not just a self-referential one -- widened 2026-08-15
    # after the narrower self-reference-only guard (still correct, still
    # in place below) made ZERO observable difference to a real,
    # confirmed-live-broken forecast. Root cause is broader than self-
    # reference: EVERY one of these three features is held FLAT across
    # the entire forecast horizon (there's no future value for a real
    # measured sensor -- see _current_measured_power()'s own docstring),
    # and a stale flat feature dominates/anchors a recursive multi-step
    # forecast toward "whatever was true at the one moment the forecast
    # cycle ran" regardless of WHICH signal it's stale for -- Battery's
    # own model using a stale grid_kw or solar_kw is just as broken as
    # using a stale battery_kw. Confirmed via two matched synthetic
    # tests: identical training data, WITHOUT any of these three
    # features reproduced a real +13kW evening plateau almost exactly
    # (12.97 vs 13.0); WITH one stale flat feature added, the same
    # model predicted -26kW (the opposite sign) for the same window.
    # A power signal's own recent behaviour is already correctly
    # captured by lag_short/lag_long (which update per-step during
    # recursive forecasting, unlike these three) -- there's no version
    # of "another signal's current value, held flat for 96 hours" that
    # is ever useful signal for a power signal's OWN forecast, only
    # ever noise. Loads are unaffected -- a load's own forecast
    # genuinely benefits from a coarse "what's the system doing right
    # now" hint, which is the intended, working use case these three
    # features were originally built for.
    @property
    def _battery_sensor(self) -> str | None:
        if self.subentry.subentry_type == SUBENTRY_TYPE_SIGNAL:
            return None
        sensor = self.entry.options.get(CONF_BATTERY_SENSOR)
        return None if sensor == self._load_sensor else sensor

    @property
    def _grid_sensor(self) -> str | None:
        if self.subentry.subentry_type == SUBENTRY_TYPE_SIGNAL:
            return None
        sensor = self.entry.options.get(CONF_GRID_SENSOR)
        return None if sensor == self._load_sensor else sensor

    @property
    def _solar_sensor(self) -> str | None:
        if self.subentry.subentry_type == SUBENTRY_TYPE_SIGNAL:
            return None
        sensor = self.entry.options.get(CONF_SOLAR_SENSOR)
        return None if sensor == self._load_sensor else sensor

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

    # Third, separate opt-in -- see const.py's CONF_EXPECTED_LOAD_KW
    # comment for the full three-mode explanation. Only takes effect in
    # predict() when both schedule bounds above are also set.
    @property
    def _expected_load_kw(self) -> float | None:
        return self.subentry.data.get(CONF_EXPECTED_LOAD_KW)

    @property
    def _allow_negative(self) -> bool:
        """True only for a "power signal" subentry (Battery/Grid/etc) --
        real, genuinely signed targets (negative = charging/exporting),
        unlike a load (physically can never draw negative power). Fixes
        a real bug found live 2026-08-15: predict()'s own clamp-to-zero,
        correct for loads, was silently zeroing every negative Battery
        prediction (i.e. every "it's charging" prediction) into a wrong
        0.0 -- see ml/model.py's own predict() docstring for the full
        story.
        """
        return self.subentry.subentry_type == SUBENTRY_TYPE_SIGNAL

    # Real, whole-house-meter entity -- the ONE load subentry that's really
    # a system-level aggregate (bleeds in battery/grid transition effects
    # the same way Battery's own midnight step does), not a genuinely
    # momentum-driven individual appliance. Deliberately hardcoded rather
    # than a new config field -- a real, confirmed-live bug (see
    # _seasonal_anchor below) needed a fast, well-scoped fix, not a new
    # UI surface; revisit as a real per-load opt-in if more than this one
    # load ever needs it.
    _WHOLE_HOUSE_ENTITY = "sensor.logger_load_power"

    @property
    def _seasonal_anchor(self) -> bool:
        """True for power-signal subentries (unchanged, existing
        behaviour) AND for the Whole House load specifically -- confirmed
        live 2026-08-15: Whole House's own recursive forecast showed a
        real, repeating ~1-1.5kW isolated spike at exactly 00:05 every
        single simulated day (1.50->3.40->2.78, 2.82->4.27->3.14, ...),
        the same recursive-lag-chain exposure-bias signature already
        root-caused and fixed for Battery/Grid/Solar earlier the same
        day -- Whole House genuinely shares a real midnight-boundary step
        (the same P2P-sell-to-self-consume automation transition that
        broke Battery's own forecast) even though it's registered as a
        "load" subentry, not a "power signal" one. Every OTHER load
        (a pool pump, an AC zone) keeps the original self-feeding lag
        behaviour unchanged -- those genuinely benefit from real near-
        term momentum carry-over in a way a system-level aggregate does
        not. Deliberately a SEPARATE flag from _allow_negative (Whole
        House still physically can't draw negative power, so its own
        predict() clamp-to-zero must stay active) -- these two properties
        answer two genuinely different questions and must never be
        conflated into one.
        """
        return (
            self.subentry.subentry_type == SUBENTRY_TYPE_SIGNAL
            or self._load_sensor == self._WHOLE_HOUSE_ENTITY
        )

    @property
    def _mode(self) -> str:
        """Which of the three real modes this load is actually in right
        now, purely from its own live config -- computed in exactly one
        place so both _async_update_data() return paths (trained and
        not-yet-trained) report the identical answer, rather than two
        copies of the same if/elif drifting apart over time.
        """
        has_schedule = (
            self._schedule_start_hour is not None
            and self._schedule_end_hour is not None
        )
        if has_schedule and self._expected_load_kw is not None:
            return "deterministic"
        if has_schedule:
            return "scheduled_ml"
        return "unscheduled"

    @property
    def _horizon_hours(self) -> int:
        return self.entry.options.get(
            CONF_FORECAST_HORIZON_HOURS, DEFAULT_FORECAST_HORIZON_HOURS
        )

    @property
    def _retrain_hour(self) -> int:
        return self.entry.options.get(
            CONF_RETRAIN_HOUR_LOCAL, DEFAULT_RETRAIN_HOUR_LOCAL
        )

    @property
    def _train_days(self) -> int:
        return self.entry.options.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS)

    @property
    def _training_source(self) -> str:
        # Deliberately a get-with-default (never assumed present) -- existing installs
        # that upgrade into this build will not have this option key at all until they
        # re-open the Forecaster options form, and the default must resolve to the
        # exact prior recorder-only behaviour so a silent upgrade never changes an
        # install's own training path.
        return self.entry.options.get(CONF_TRAINING_SOURCE, DEFAULT_TRAINING_SOURCE)

    @property
    def _hybrid_recent_days(self) -> int:
        return self.entry.options.get(
            CONF_HYBRID_RECENT_DAYS, DEFAULT_HYBRID_RECENT_DAYS
        )

    # -- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load a persisted model if present, then wire up the nightly retrain."""
        self._trained = await self.hass.async_add_executor_job(
            self._load_model_from_disk
        )
        self._residuals = await self.hass.async_add_executor_job(
            self._load_residuals_from_disk
        )

        self._unsub_retrain = async_track_time_change(
            self.hass,
            self._handle_retrain_trigger,
            hour=self._retrain_hour,
            minute=0,
            second=0,
        )

        if self._trained is None:
            # Nothing on disk yet -- train immediately (in the background,
            # NOT awaited here) so the sensor has real data soon after setup,
            # instead of sitting empty for up to 24h waiting for the next
            # scheduled retrain hour. Same "kick it off, don't block setup"
            # pattern __init__.py already uses for the Solver's own first
            # cycle (hass.async_create_task(solver_runtime.async_run_solve)).
            #
            # Real, live-reproduced bug this fixes (2026-08-26, devhub): this
            # used to be `await self._async_retrain()`, awaited INLINE inside
            # __init__.py's own per-subentry setup loop, before
            # async_forward_entry_setups() (i.e. before ANY entity gets
            # registered) ever runs. _async_retrain() does several sequential
            # recorder history fetches plus a real ML training job -- on an
            # install with several subentries simultaneously lacking a
            # persisted model (e.g. right after a `.pkl` reset, or several
            # subentries added at once), that blocking chain can genuinely
            # take minutes, comfortably risking HA's own slow-setup timeout.
            # Confirmed live: a real "Platform nimbus_load does not generate
            # unique IDs" ERROR burst (every hub-level number/switch/sensor
            # entity duplicate-registering) landed at 19:00-19:01 the same
            # night devhub had several untrained subentries -- consistent
            # with HA abandoning/retrying a setup that ran long, while the
            # original attempt's still-executing training work (executor
            # jobs don't get interrupted by task cancellation) finished and
            # tried to register the same entities a second time. A plain
            # isolated reload_config_entry AND a full restart both failed to
            # reproduce it once every subentry already had a persisted model
            # on disk -- consistent with training time, not reload/restart
            # itself, being the real trigger. _async_update_data() already
            # returns a well-defined, already-exercised "untrained" state
            # dict (state=None, forecast=[], training_points=0) whenever
            # self._trained is None, so nothing downstream needs the
            # training to have finished by the time hub setup completes --
            # this was blocking behaviour with no correctness reason behind
            # it, only a "get real data displayed a bit sooner" one, which a
            # background task still satisfies.
            #
            # Idempotent (2026-08-27, nimbus repo issue #211): cancel any
            # retrain task already tracked for this subentry_id before
            # scheduling a new one -- see _retrain_tasks' own module-level
            # comment for why a second, independent coordinator object for
            # the same subentry otherwise leaves an orphaned, untracked
            # retrain running forever.
            old_task = _retrain_tasks.pop(self.subentry.subentry_id, None)
            if old_task is not None and not old_task.done():
                old_task.cancel()
            _retrain_tasks[self.subentry.subentry_id] = self.hass.async_create_task(
                self._async_retrain()
            )

    def async_unload(self) -> None:
        if self._unsub_retrain is not None:
            self._unsub_retrain()
            self._unsub_retrain = None
        old_task = _retrain_tasks.pop(self.subentry.subentry_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

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

            load_events = await self._async_fetch_training_history(
                self._load_sensor, start, end, convert_power=True
            )
            # Real bug found live (2026-08-31): all six of the calls below used
            # to read `_async_fetch_history`, a name that stopped existing the
            # moment #257/#259 (2026-08-28) renamed it to
            # `_async_fetch_recorder_history` and introduced this training-
            # source-aware wrapper -- only the load_events call above got
            # migrated at the time, leaving these six still pointed at a
            # nonexistent method. Every subentry with ANY of temp/humidity/
            # curtailment/battery/grid/solar configured (temp/humidity are
            # shared HUB-level options, so this is effectively every
            # subentry on a real install) hit an immediate AttributeError
            # here, before train_model() was ever reached -- explaining why
            # neither of train_model()'s own "no history"/"too few points"
            # warnings ever appeared for an affected subentry while
            # training_points stayed 0 forever. A subentry with a
            # pre-#257 .pkl already on disk masked this silently: _trained
            # is not None, so async_setup() never calls _async_retrain() at
            # startup, and the crash only bites (also silently -- an
            # unhandled exception inside a create_task) at the next
            # scheduled nightly retrain.
            temp_events = (
                await self._async_fetch_training_history(
                    self._temp_sensor, start, end
                )
                if self._temp_sensor
                else []
            )
            humidity_events = (
                await self._async_fetch_training_history(
                    self._humidity_sensor, start, end
                )
                if self._humidity_sensor
                else []
            )
            curtailment_events = (
                await self._async_fetch_training_history(
                    self._curtailment_sensor, start, end, binary=True
                )
                if self._curtailment_sensor
                else []
            )
            # Real measured power history only -- convert_power=True since
            # a real inverter/meter sensor can report W (this household's
            # own solar sensor does) while a sibling reports kW; never an
            # optimizer's own plan/forecast entity.
            battery_events = (
                await self._async_fetch_training_history(
                    self._battery_sensor, start, end, convert_power=True
                )
                if self._battery_sensor
                else []
            )
            grid_events = (
                await self._async_fetch_training_history(
                    self._grid_sensor, start, end, convert_power=True
                )
                if self._grid_sensor
                else []
            )
            solar_events = (
                await self._async_fetch_training_history(
                    self._solar_sensor, start, end, convert_power=True
                )
                if self._solar_sensor
                else []
            )

            trained = await self.hass.async_add_executor_job(
                _train_model_job,
                load_events,
                temp_events,
                humidity_events,
                curtailment_events,
                start,
                end,
                self._schedule_start_hour,
                self._schedule_end_hour,
                battery_events,
                grid_events,
                solar_events,
            )
            if trained is not None:
                self._trained = trained
                await self.hass.async_add_executor_job(
                    self._save_model_to_disk, trained
                )
                await self.async_request_refresh()
        finally:
            self._retraining = False

    # -- data access ----------------------------------------------------------

    async def _async_fetch_training_history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        convert_power: bool = False,
        binary: bool = False,
    ) -> list[tuple[datetime, float]]:
        """Fetch training-window history for one entity, honouring the configured
        training_source (recorder / lts / hybrid).

        binary=True forces recorder-only regardless of setting -- the LTS path returns
        hourly means (0.0-1.0), which is meaningless for a switch state ("was the
        curtailment sensor on 27% of this hour?" is not what the training grid asks
        for). recorder still purges the same way for a binary sensor as for a numeric
        one, so this is a real limitation, not a design choice -- documented rather
        than silently degraded.

        The recorder-only path is unchanged and is the safe default: existing
        installs that upgrade into this build see identical behaviour until they
        opt into lts or hybrid via the options form.
        """
        source = self._training_source
        if binary or source == TRAINING_SOURCE_RECORDER:
            return await self._async_fetch_recorder_history(
                entity_id, start, end, convert_power=convert_power, binary=binary
            )
        if source == TRAINING_SOURCE_LTS:
            return await self._async_fetch_lts_history(
                entity_id, start, end, convert_power=convert_power
            )
        if source == TRAINING_SOURCE_HYBRID:
            # Recent slice from recorder (full resolution), older slice from LTS
            # (hourly aggregates), joined at recent_start with the recorder points
            # winning any overlap in the interior of the recent window.
            recent_days = self._hybrid_recent_days
            recent_start = end - timedelta(days=recent_days)
            # Guard against a config with recent_days >= train_days -- degrades
            # gracefully to pure recorder rather than fetching an empty LTS range.
            if recent_start <= start:
                return await self._async_fetch_recorder_history(
                    entity_id, start, end, convert_power=convert_power
                )
            recent = await self._async_fetch_recorder_history(
                entity_id, recent_start, end, convert_power=convert_power
            )
            older = await self._async_fetch_lts_history(
                entity_id, start, recent_start, convert_power=convert_power
            )
            # Older-then-recent, both already tz-aware local. resample_last_value()
            # in ml/model.py assumes monotonic timestamps -- both lists individually
            # are, and older's last timestamp is strictly < recent's first (the
            # recent_start boundary is exclusive on the LTS side by construction:
            # statistics_during_period's `end_time` is exclusive).
            return older + recent
        _LOGGER.warning(
            "Unknown training_source %r for %s -- falling back to recorder",
            source,
            entity_id,
        )
        return await self._async_fetch_recorder_history(
            entity_id, start, end, convert_power=convert_power, binary=binary
        )

    async def _async_fetch_recorder_history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        convert_power: bool = False,
        binary: bool = False,
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
                    out.append(
                        (
                            dt_util.as_local(s.last_changed),
                            1.0 if s.state == "on" else 0.0,
                        )
                    )
                    continue
                try:
                    value = float(s.state)
                except (TypeError, ValueError):
                    continue
                if convert_power:
                    unit = s.attributes.get("unit_of_measurement")
                    if unit and unit != UnitOfPower.KILO_WATT:
                        try:
                            value = PowerConverter.convert(
                                value, unit, UnitOfPower.KILO_WATT
                            )
                        except Exception:  # noqa: BLE001 -- an unrecognized unit string must degrade to "treat as kW", never crash the coordinator
                            if not warned_missing_unit:
                                _LOGGER.warning(
                                    "%s reported unconvertible unit '%s' -- treating as kW as-is",
                                    entity_id,
                                    unit,
                                )
                                warned_missing_unit = True
                    # Real, live-confirmed bug (2026-08-17): a single bad
                    # Modbus read in this household's real recorder history
                    # (sensor.logger_meter_total_active_power, 2026-07-07,
                    # value 21474836.5 -- a classic 32-bit signed-integer-
                    # overflow artifact, 2^31/100) sat harmlessly in
                    # recorder history for over a month until a full
                    # cold retrain genuinely re-read it as real training
                    # data. k-NN's own prediction is mathematically bounded
                    # by real y_train values (a weighted average can never
                    # exceed the observed range) -- which is exactly why a
                    # later recursive forecast step, once its own feature
                    # vector happened to land near that one poisoned
                    # training point, correctly (if uselessly) reproduced
                    # something close to 21 million kW. The bound guarantee
                    # protects against extrapolation, not against bad DATA
                    # inside the bound. MAX_SANE_POWER_KW is comfortably
                    # above any real signal this household (or any
                    # realistic household) could ever produce (documented
                    # real ceiling here is ~50kW for Grid/Battery, ~17kW
                    # for Solar) while safely far below the magnitude any
                    # unit-conversion/integer-overflow-class glitch
                    # produces (these tend to be in the thousands to
                    # millions, never a plausible near-miss on a real
                    # value) -- dropped entirely (never trained on, never
                    # displayed), same as an unparseable state already is.
                    if abs(value) > MAX_SANE_POWER_KW:
                        _LOGGER.warning(
                            "%s reported a physically implausible power reading (%.1f kW, "
                            "exceeds the %.0f kW sanity ceiling) at %s -- dropping this one "
                            "point rather than training on it",
                            entity_id,
                            value,
                            MAX_SANE_POWER_KW,
                            s.last_changed,
                        )
                        continue
                out.append((dt_util.as_local(s.last_changed), value))
            return out

        return await get_instance(self.hass).async_add_executor_job(_fetch)

    async def _async_fetch_lts_history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        convert_power: bool = False,
    ) -> list[tuple[datetime, float]]:
        """Fetch hourly long-term-statistics for one entity, in-process, via the
        recorder's own executor -- same convention as _async_fetch_recorder_history.

        LTS is populated by the recorder daemon for any sensor with
        state_class=measurement (or total/total_increasing) and a unit -- which
        includes every power sensor Nimbus already trains on -- and survives
        recorder's own purge, so 30/90/365-day retrains are possible on an install
        whose raw states table only retains a few days. Confirmed live 2026-08-28
        on Mark Purcell's install: 30 days / 720 hourly rows for
        sensor.sigen_plant_consumed_power against a recorder that had only retained
        the last 5.9 days of raw states.

        Returns one (timestamp, mean_kW) tuple per hour bucket, timestamped at the
        bucket START in local time -- matching the tz-aware-local convention of the
        recorder path so ml/model.py's resample_last_value() and the seasonal-lookup
        bucketing can treat both origins identically.

        convert_power converts the returned mean to kW via PowerConverter, reading
        the entity's own configured unit_of_measurement (LTS doesn't store per-row
        units -- statistics.statistics_meta records one unit per statistic_id,
        recorded once when the statistic was first created). Falls back to
        already-kW if the unit is missing or unconvertible, same as the recorder
        path already does.

        Returns [] on a genuine LTS-empty result (a fresh install where recorder has
        never rolled up a statistic for this entity yet, or an entity that isn't
        eligible for LTS at all) -- lets the caller treat it the same as the
        recorder path's own "no history yet" case.
        """
        # Read the recorded unit ONCE, from the current state's own attributes --
        # LTS stores metadata separately but that's a private API; the current
        # state's unit is the same unit the LTS rows were recorded against for any
        # sensor whose unit hasn't changed mid-history, which is the overwhelming
        # common case. If the unit HAS changed (e.g. a sensor swapped from W to kW
        # partway through), the same limitation applies to the recorder path today
        # -- documented as a known edge case rather than silently mis-scaled here.
        unit: str | None = None
        if convert_power:
            state = self.hass.states.get(entity_id)
            if state is not None:
                unit = state.attributes.get("unit_of_measurement")

        warned_missing_unit = False

        def _fetch() -> list[tuple[datetime, float]]:
            nonlocal warned_missing_unit
            # period="hour" is the LTS bucket kept indefinitely by recorder.
            # types={"mean"} is what a load/power sensor's training grid actually
            # consumes -- min/max/change/sum are useful for a diagnostic view but
            # unused by ml/model.py's own resample.
            raw = statistics_during_period(
                self.hass,
                start,
                end,
                {entity_id},
                "hour",
                None,  # units -- None = raw, we convert ourselves for parity with the recorder path
                {"mean"},
            )
            rows = raw.get(entity_id, [])
            out: list[tuple[datetime, float]] = []
            for r in rows:
                mean = r.get("mean")
                if mean is None:
                    continue
                value = float(mean)
                if convert_power and unit and unit != UnitOfPower.KILO_WATT:
                    try:
                        value = PowerConverter.convert(
                            value, unit, UnitOfPower.KILO_WATT
                        )
                    except Exception:  # noqa: BLE001 -- match recorder path: degrade to "treat as kW", never crash
                        if not warned_missing_unit:
                            _LOGGER.warning(
                                "%s LTS reports unconvertible unit '%s' -- treating as kW as-is",
                                entity_id,
                                unit,
                            )
                            warned_missing_unit = True
                if abs(value) > MAX_SANE_POWER_KW:
                    # Same sanity guard as the recorder path -- a poisoned raw
                    # state can propagate into an LTS row's mean the same way it
                    # can into a raw state read (an hourly mean of 21 million kW
                    # is just as physically implausible as an individual sample
                    # at 21 million kW). Drop the point rather than train on it.
                    _LOGGER.warning(
                        "%s LTS reports a physically implausible mean (%.1f kW, "
                        "exceeds the %.0f kW sanity ceiling) at %s -- dropping this one "
                        "row rather than training on it",
                        entity_id,
                        value,
                        MAX_SANE_POWER_KW,
                        r.get("start"),
                    )
                    continue
                # LTS row "start" is a UTC datetime object (or ISO string in older
                # HA cores); normalize both, then convert to local for parity with
                # the recorder path's dt_util.as_local(s.last_changed).
                ts_raw = r.get("start")
                if isinstance(ts_raw, str):
                    ts_utc = datetime.fromisoformat(ts_raw)
                else:
                    ts_utc = ts_raw
                if ts_utc is None:
                    continue
                out.append((dt_util.as_local(ts_utc), value))
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

    def _current_measured_power(self, entity_id: str | None) -> float:
        """Real measured battery/grid/solar power (kW), held flat across
        the whole forecast horizon. Unlike temperature (has a real
        forecast source) or curtailment (sometimes does), none of these
        three have any forward-looking source at all without borrowing an
        optimizer's own plan -- which this integration never does (see
        this repo's own CLAUDE.md PRIME DIRECTIVE). Holding the current
        real reading flat is an honest, explicitly-scoped approximation,
        not a claim of future accuracy -- genuinely wrong the moment the
        real value changes materially within the horizon, same trade-off
        this integration already accepts for humidity.

        Converts unit via PowerConverter same as history fetches
        (convert_power=True) -- confirmed live 2026-08-15 that a real
        solar sensor on this system reports W while battery/grid sensors
        report kW, so this can't assume kW unconditionally.
        """
        if entity_id is None:
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is None:
            return 0.0
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return 0.0
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfPower.KILO_WATT:
            try:
                value = PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)
            except Exception:  # noqa: BLE001 -- same reasoning as the sibling catch above: degrade to kW-as-is, never crash
                _LOGGER.warning(
                    "%s reported unconvertible unit '%s' -- treating as kW as-is",
                    entity_id,
                    unit,
                )
        return value

    async def _async_fetch_temperature_forecast(self) -> list[tuple[datetime, float]]:
        """Two real, independently-shaped sources feed this, per #123
        (Mark Purcell, real repro, 2026-08-24): a plain `sensor.*`
        template that already publishes its own `forecast` attribute
        (the original, still-supported path -- e.g. a household's own
        pre-built trigger-template sensor), or a `weather.*` entity
        directly -- confirmed live (HA core source,
        homeassistant/components/weather/__init__.py) that modern HA
        (2024.x+) no longer exposes a `forecast` state attribute on
        weather entities at all; the only way to reach one is the
        `weather.get_forecasts` service call
        (`supports_response=SupportsResponse.ONLY`). Both paths
        converge on the exact same downstream parsing loop below --
        confirmed via HA core's own `ATTR_FORECAST_TIME`/
        `ATTR_FORECAST_TEMP` constants ('datetime'/'temperature') that
        the service's own response entries use identical field names to
        what the sensor-attribute path already expected, so nothing
        past the "get me a raw list of {datetime, temperature} dicts"
        step needed to change."""
        if self._temp_forecast_sensor is None:
            return []
        entity_id = self._temp_forecast_sensor
        domain = entity_id.split(".", 1)[0]
        if domain == "weather":
            forecast = await self._async_fetch_weather_forecast_via_service(entity_id)
        else:
            state = self.hass.states.get(entity_id)
            forecast = state.attributes.get("forecast", []) if state else []
        out: list[tuple[datetime, float]] = []
        for entry in forecast:
            try:
                ts = dt_util.parse_datetime(entry["datetime"])
                temp = float(entry["temperature"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts is not None:
                out.append((_normalize_forecast_timestamp(ts), temp))
        out.sort(key=lambda x: x[0])

        # #269 mitigation A -- cache the last real, non-empty forecast for
        # THIS specific entity_id, and fall back to a trimmed (future-only)
        # slice of it whenever a fetch comes back empty. A genuinely
        # non-empty result always refreshes the cache; an empty one only
        # ever reads from it -- never both in the same call, so a cached
        # value can't get overwritten with the empty result that's about
        # to be replaced by it.
        if out:
            self._temp_forecast_cache = out
            self._temp_forecast_cache_entity = entity_id
        cached: list[tuple[datetime, float]] = []
        if not out and self._temp_forecast_cache_entity == entity_id:
            now = dt_util.utcnow()
            cached = [pair for pair in self._temp_forecast_cache if pair[0] >= now]
        effective = out or cached

        # #269 mitigation B -- warn on every genuine success->failure
        # transition (not just the very first tick ever), and log an
        # INFO on every failure->success recovery, so a household
        # triaging a report can tell whether it recovered on its own.
        # Deliberate, verified improvement over Mark's own literal sketch
        # (which treated the very first-ever fetch as "no state change to
        # log" regardless of outcome): a household whose configured
        # sensor has NEVER once worked would otherwise never see a single
        # warning under that literal reading, which is a real regression
        # from the previous one-shot-on-first-empty-result behaviour this
        # is replacing. First tick still logs a real warning if it's
        # already failing; only a genuinely NEW value (not None) skips
        # logging when the state hasn't changed.
        now_ok = bool(effective)
        was_ok = self._last_temp_forecast_ok
        if was_ok is None:
            if not now_ok:
                _LOGGER.warning(
                    "temperature_forecast_sensor '%s' is configured but yielded 0 "
                    "forecast entries -- the temperature feature will train as "
                    "dead weight this cycle. If this is a weather.* entity, "
                    "confirm it actually supports hourly forecasts (some "
                    "integrations only support daily); if it's a sensor.* "
                    "template, confirm its 'forecast' attribute is a real, "
                    "non-empty list shaped like [{'datetime': ..., "
                    "'temperature': ...}, ...].",
                    entity_id,
                )
        elif was_ok and not now_ok:
            _LOGGER.warning(
                "temperature_forecast_sensor '%s' stopped yielding forecast "
                "entries (previously successful) -- the temperature feature "
                "will train as dead weight until this recovers.",
                entity_id,
            )
        elif not was_ok and now_ok:
            _LOGGER.info(
                "temperature_forecast_sensor '%s' recovered and is yielding "
                "%d forecast entries again.",
                entity_id,
                len(effective),
            )
        self._last_temp_forecast_ok = now_ok
        return effective

    async def _async_fetch_weather_forecast_via_service(
        self, entity_id: str
    ) -> list[dict[str, Any]]:
        """Calls weather.get_forecasts (type='hourly') for one entity and
        unwraps its response. Confirmed live (HA core source,
        homeassistant/helpers/service.py's entity_service_call, line
        ~1032) that a single-entity target's result is keyed by its own
        entity_id in the aggregated response --
        {entity_id: {"forecast": [...]}} -- not returned flat, matching
        exactly what a real YAML `response_variable:` capture of this
        same service call looks like.

        Wrapped defensively (real, not theoretical, failure modes this
        service call can hit that a plain attribute read never could):
        the entity might not support hourly forecasts at all (some
        integrations only implement daily -- HomeAssistantError),
        might not exist / might not be a real weather entity
        (ServiceNotFound / vol.Invalid on the target), or the service
        call infrastructure itself might be transiently unavailable.
        None of these should ever crash a coordinator tick -- same
        honest-fallback-over-crash discipline as every other real
        external-data fetch in this file -- they degrade to "no
        temperature forecast this cycle" (caught by the empty-result
        warning in the caller) rather than taking the whole retrain/
        predict cycle down with them."""
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception:
            # A real external service call (unsupported forecast type,
            # entity gone, transient HA-core issue) must never take down
            # a coordinator tick -- degrades to "no forecast this cycle"
            # instead, surfaced via the caller's own empty-result
            # warning. No lint suppression comment needed here (unlike
            # the two sibling blind-except sites elsewhere in this
            # file) -- confirmed directly that ruff's blind-except rule
            # doesn't flag a broad except when the log call includes
            # exc_info=True, which this one already does.
            _LOGGER.warning(
                "weather.get_forecasts failed for '%s' -- treating as no "
                "forecast data this cycle.",
                entity_id,
                exc_info=True,
            )
            return []
        if not response:
            return []
        entity_result = response.get(entity_id, {})
        forecast = entity_result.get("forecast", [])
        if not isinstance(forecast, list):
            return []
        return forecast

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
                out.append((_normalize_forecast_timestamp(ts), value))
        out.sort(key=lambda x: x[0])
        return out

    # -- model persistence ----------------------------------------------------

    def _load_model_from_disk(self) -> TrainedModel | None:
        if not self._model_path.exists():
            return None
        try:
            trained = pickle.loads(self._model_path.read_bytes())
        except Exception:
            _LOGGER.warning(
                "Could not load persisted model, will retrain.", exc_info=True
            )
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
                self.subentry.subentry_id,
                trained.x_mean.shape[0],
                len(FEATURE_NAMES),
            )
            return None
        return trained

    def _save_model_to_disk(self, trained: TrainedModel) -> None:
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._model_path.write_bytes(pickle.dumps(trained))

    def _load_residuals_from_disk(self) -> list[float]:
        if not self._residual_path.exists():
            return []
        try:
            data = json.loads(self._residual_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and all(
                isinstance(v, (int, float)) for v in data
            ):
                return [float(v) for v in data]
        except Exception:
            _LOGGER.warning(
                "Could not load persisted residual buffer, starting fresh.",
                exc_info=True,
            )
        return []

    def _save_residuals_to_disk(self) -> None:
        self._residual_path.parent.mkdir(parents=True, exist_ok=True)
        self._residual_path.write_text(json.dumps(self._residuals), encoding="utf-8")

    def _check_residual_drift(self) -> None:
        """Anomaly layer (2026-08-25): analyzes the SAME rolling residual
        buffer already maintained for confidence-band calibration --
        no new data collection, just a second, cheap pass over data this
        coordinator already computes every cycle. See anomaly.py's own
        module docstring for the full "why this, grounded in real bug
        history" reasoning. Strictly observational: logs a WARNING
        (which the health report already surfaces via health.py's log
        buffer, zero new plumbing needed) and never raises, never
        touches self._trained or the published forecast.
        """
        try:
            self._residual_drift_status = residual_drift_status(self._residuals)
            anomaly = detect_residual_drift(self._residuals)
        except Exception:
            _LOGGER.warning(
                "%s: residual drift check itself failed -- skipping this cycle",
                self.subentry.title,
                exc_info=True,
            )
            return
        if anomaly is None:
            self._residual_drift_flagged = False
            return
        if not self._residual_drift_flagged:
            _LOGGER.warning(
                "%s: forecast residual drift detected -- recent one-step error "
                "(%.3f) is %.1fx this signal's own recent baseline (%.3f). This "
                "does not affect the published forecast, but may indicate model "
                "degradation, sensor drift, or new data contamination (e.g. "
                "curtailment) -- worth a look.",
                self.subentry.title,
                anomaly.recent_mean_error,
                anomaly.ratio,
                anomaly.baseline_mean_error,
            )
            self._residual_drift_flagged = True

    # -- coordinator tick (cheap: inference only) ------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if self._trained is None:
            return {
                "state": None,
                "forecast": [],
                "mode": self._mode,
                "trained_at": None,
                "training_points": 0,
                "model_type": None,
                "validation_mae": {},
                "validation_mase": {},
                "mase_scale_points": 0,
                "resample_minutes": 0,
                "training_span_days": 0.0,
                "residual_drift_status": asdict(self._residual_drift_status),
            }

        now_utc = dt_util.utcnow()
        horizon_end = now_utc + timedelta(hours=self._horizon_hours)

        temp_forecast = await self._async_fetch_temperature_forecast()
        fallback_temp = (
            temp_forecast[0][1] if temp_forecast else DEFAULT_FALLBACK_TEMPERATURE_C
        )
        current_humidity = self._current_humidity()
        curtailment_forecast = await self._async_fetch_curtailment_forecast()
        current_battery_kw = self._current_measured_power(self._battery_sensor)
        current_grid_kw = self._current_measured_power(self._grid_sensor)
        current_solar_kw = self._current_measured_power(self._solar_sensor)

        # Real recent history, not just the model's own state -- this is
        # the lag-feature seed. Fetched with a comfortable multiple of
        # margin past LAG_LONG_STEPS so a real value is always available at
        # the very first forecast step even if the recorder's most recent
        # write is a few minutes stale.
        lag_lookback = timedelta(minutes=RESAMPLE_MINUTES * (LAG_LONG_STEPS + 4))
        # Lag features intentionally use the RAW recorder path, never LTS, regardless
        # of the training_source -- lag_short is "what was the load doing 15 minutes
        # ago" and an hourly LTS mean cannot answer that. The recent lag lookback
        # window is ~1 hour, always well inside any sane recorder retention.
        recent_load_values = await self._async_fetch_recorder_history(
            self._load_sensor, now_utc - lag_lookback, now_utc, convert_power=True
        )

        # Resolve the LAST cycle's near-term prediction against what
        # actually happened, now that real time has caught up to it --
        # this is the confidence-band calibration data (ml/model.py's
        # calibrated_band()). One-shot per cycle: whether or not a real
        # comparison point was found, don't carry a stale pending
        # prediction into a future cycle.
        if self._last_step_prediction is not None:
            pred_time, pred_value = self._last_step_prediction
            if pred_time <= now_utc:
                sorted_recent = sorted(recent_load_values, key=lambda p: p[0])
                recent_times = [p[0] for p in sorted_recent]
                idx = bisect_right(recent_times, pred_time) - 1
                if idx >= 0:
                    actual_value = sorted_recent[idx][1]
                    self._residuals.append(abs(pred_value - actual_value))
                    if len(self._residuals) > MAX_RESIDUALS_STORED:
                        del self._residuals[0]
                    await self.hass.async_add_executor_job(self._save_residuals_to_disk)
                    self._check_residual_drift()
            self._last_step_prediction = None

        timestamps: list[datetime] = []
        temps: list[float] = []
        humidities: list[float] = []
        curtailments: list[float] = []
        batteries_kw: list[float] = []
        grids_kw: list[float] = []
        solars_kw: list[float] = []
        t = now_utc
        step = timedelta(minutes=RESAMPLE_MINUTES)
        while t <= horizon_end:
            timestamps.append(dt_util.as_local(t))
            temps.append(_nearest_temp(temp_forecast, t, fallback_temp))
            humidities.append(current_humidity)
            curtailments.append(_step_lookup(curtailment_forecast, t, 0.0))
            # Held flat at the current real reading -- no forward-looking
            # source exists for any of these three without borrowing an
            # optimizer's own plan, which this integration never does.
            batteries_kw.append(current_battery_kw)
            grids_kw.append(current_grid_kw)
            solars_kw.append(current_solar_kw)
            t += step

        result: PredictionResult = await self.hass.async_add_executor_job(
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
            self._expected_load_kw,
            batteries_kw,
            grids_kw,
            solars_kw,
            self._allow_negative,
            self._seasonal_anchor,
        )
        preds = result.values
        has_model_bounds = (
            result.model_lower is not None and result.model_upper is not None
        )

        # Real, observed physical range for this signal/load -- confirmed
        # live 2026-08-15 that calibrated_band()'s sqrt(1+lead_hours)
        # growth has no ceiling at all: for Grid (whose GBRT candidate
        # badly overfit -- validation_mae=6220 vs knn's 7.55, so k-NN won
        # and there's no bounded quantile model, forcing the residual
        # fallback), the band had widened to +/-100kW by 96h out, nearly
        # 2.5x this project's own long-documented ~44kW physical grid
        # limit, and visibly still growing ("spirals up and up," reported
        # live). Genuine model-derived quantile bounds (has_model_bounds)
        # aren't immune to the same risk either -- a tree-based quantile
        # model can still extrapolate past its own training range for a
        # timestamp/feature combination it never saw. Clamping BOTH
        # sources to what this signal has actually, really done (with a
        # modest 20% margin -- a genuine future extreme shouldn't be
        # impossible, just not unboundedly implausible) is honest in a
        # way an arbitrary fixed kW constant wouldn't be: it's grounded
        # in this specific signal's own real data, works identically for
        # a 3kW load and a 44kW battery with zero per-entity tuning.
        y_min = float(np.min(self._trained.y_train))
        y_max = float(np.max(self._trained.y_train))
        y_range = y_max - y_min
        margin = y_range * 0.2 if y_range > 1e-9 else max(abs(y_max), 1.0) * 0.2
        bound_floor = y_min - margin
        bound_ceiling = y_max + margin

        points = []
        for i, (ts, v) in enumerate(zip(timestamps, preds, strict=True)):
            if has_model_bounds:
                # Genuine model-derived quantile bounds (GBRT winner, real
                # validation set to early-stop against) -- preferred over
                # the residual-based fallback below whenever available,
                # never blended with it for a single load's forecast.
                lower, upper = result.model_lower[i], result.model_upper[i]
            else:
                lead_hours = (ts - now_utc).total_seconds() / 3600
                band = calibrated_band(self._residuals, v, lead_hours)
                lower = (v - band) if self._allow_negative else max(0.0, v - band)
                upper = v + band
            lower = max(lower, bound_floor)
            upper = min(upper, bound_ceiling)
            points.append(
                {
                    "time": ts.isoformat(),
                    "value": round(v, 3),
                    "lower": round(lower, 3),
                    "upper": round(upper, 3),
                }
            )
        current = preds[0] if preds else 0.0

        # Remember this cycle's near-term point so the NEXT cycle can
        # resolve it against reality once real time has caught up.
        if timestamps and preds:
            self._last_step_prediction = (timestamps[0], preds[0])

        return {
            "state": round(current, 3),
            "forecast": points,
            "mode": self._mode,
            "trained_at": self._trained.trained_at.isoformat(),
            "training_points": self._trained.training_points,
            # getattr-defensive, same reasoning as mase_scale_points/resample_minutes/
            # training_span_days below (nimbus issue #196): model_type has existed on
            # TrainedModel since before this field was ever surfaced here, so every
            # currently-persisted .pkl already has it -- but a future TrainedModel field
            # added the same way should follow this same defensive pattern from day one,
            # not direct attribute access, until proven safe against every deployed .pkl.
            "model_type": getattr(self._trained, "model_type", None),
            "validation_mae": self._trained.validation_mae,
            "validation_mase": self._trained.validation_mase,
            # getattr-defensive (not direct attribute access) -- these
            # three fields are new (nimbus issue #113); a .pkl persisted
            # by a pre-fix version unpickles with them genuinely absent,
            # same documented class of bug seasonal_lookup already hit
            # (see model.py's own predict() for the full "why").
            "mase_scale_points": getattr(self._trained, "mase_scale_points", 0),
            "resample_minutes": getattr(self._trained, "resample_minutes", 0),
            "training_span_days": getattr(self._trained, "training_span_days", 0.0),
            "residual_drift_status": asdict(self._residual_drift_status),
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
    battery_events: list[tuple[datetime, float]],
    grid_events: list[tuple[datetime, float]],
    solar_events: list[tuple[datetime, float]],
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
        battery_events=battery_events,
        grid_events=grid_events,
        solar_events=solar_events,
    )


def _parse_time_to_hour(value: str | float | None) -> float | None:
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
        _LOGGER.warning(
            "Could not parse schedule time value %r -- treating as unset", value
        )
        return None


def _normalize_forecast_timestamp(ts: datetime) -> datetime:
    """Real bug (Mark Purcell, #137, real repro, 2026-08-24, direct
    follow-up to #123): `dt_util.parse_datetime()` on a genuinely naive
    ISO-8601 string (no `+HH:MM`/`Z` suffix) returns a naive datetime --
    confirmed live, `weather.noosa_heads_hourly` (a real, community
    OpenWeatherMap-derived HA integration) emits exactly this shape from
    `weather.get_forecasts`, in LOCAL wall-clock time (his own
    confirmation: "15:00" genuinely means 15:00 AEST on his install, not
    15:00 UTC -- matched against real live weather at the time). Every
    `target` this gets compared against (via `bisect_right` in
    `_nearest_temp`/`_step_lookup` below) is always tz-aware, and Python
    refuses to compare a naive and an aware datetime at all -- a hard
    `TypeError`, not a silently-wrong comparison, which is exactly what
    crashed his coordinator every tick once this path was reachable.

    Real, honest ambiguity, called out directly rather than assumed
    away: HA's own weather-platform contract says forecast datetimes
    SHOULD be UTC, but (per Mark's own finding) not every real
    integration honours that, and a genuinely naive value carries no
    signal either way about which convention its own author followed.
    "Assume local" is the pragmatic default shipped here (matching
    Mark's own reasoning: the alternative risks a real multi-hour
    misalignment on exactly the installs most likely to hit this path
    at all -- a community integration emitting naive datetimes in the
    first place).

    Deliberately NOT `dt_util.as_local(ts)` for the naive case, despite
    that being Mark's own first suggested fix -- a real, subtle bug in
    that suggestion, caught before shipping it: HA's real `as_local()`
    (confirmed via its own source/docstring, "Convert a UTC datetime
    object to local time zone") treats ANY naive input as already being
    UTC, converting it FORWARD to local wall-clock time -- which for a
    genuinely local-naive "15:00" input would shift it to "01:00 next
    day" in AEST (+10h), the exact class of misalignment this fix
    exists to prevent, just reproduced in a different form. The correct
    operation for "these numbers are already the right local wall-clock
    time, just missing the tag" is a pure relabel (`.replace(tzinfo=
    ...)`, no numeric conversion at all), not a timezone CONVERSION
    (`as_local()`/`as_utc()`, which do shift the numbers) -- confirmed
    by tracing HA's own real implementation, not assumed.

    `dt_util.DEFAULT_TIME_ZONE` is HA's own real, live-configured local
    timezone (set from `hass.config.time_zone` at startup) -- genuinely
    reflects THIS specific installation's own local time, not a
    hardcoded assumption. Already-aware timestamps (the common,
    correctly-behaving case -- confirmed via this repo's own existing
    tests, and per HA's documented platform contract) pass through
    completely unchanged.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return ts


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
