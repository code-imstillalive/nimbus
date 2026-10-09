"""`sensor.nimbus_p5min_forecast`: AEMO's 5-minute pre-dispatch for this
install's NEM region, fetched by Nimbus itself every five minutes.

nimbus #1653 / #1658. Nothing a NEM install normally has carries AEMO's
5-minute pre-dispatch, and on 9 Oct 2026 it was the only published forecast
that saw the 05:20 spike coming. This publishes it for every NEM install
without a separate integration. It does NOT feed the solve: what the solver
does with it is a separate, reviewed change (#1653).

Region: from the AEMO sensors already configured on the Solver
(`sensor.nem_pd7day_qld1_...`), else the Companion App's geocoded location
(#495). No region, no fetch -- a non-NEM install makes no outbound call.

State: the price of the interval in progress, $/kWh. `forecast` holds every
row (about an hour) and is excluded from Recorder; so is the previous run's,
kept so a consumer can ask whether two consecutive runs agree.

`readiness_signal` is #1660's rule (two consecutive runs forecasting
>= $500/MWh, held at most 60 minutes), in SHADOW: published so it can be
checked against events and quiet mornings, never read by the solve.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import UTC, datetime, timedelta

import aiohttp
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from . import p5min, sensor_discovery
from .const import (
    CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
POLL = timedelta(minutes=1)
# AEMO publishes run R+5 about 3m40s before R+5, i.e. ~R+1m20s. The listing
# (~110 KB) is not read before then, so a normal run costs one listing plus
# one zip (~220 KB): about 95 MB a day.
_NEXT_RUN_DUE = timedelta(minutes=1, seconds=15)
_TIMEOUT = aiohttp.ClientTimeout(total=20)


def resolve_region(hass: HomeAssistant, entry) -> str | None:
    """The configured AEMO sensors' region; the geocoded location only when
    they name none. Sensors naming two regions is a contradiction, not an
    absence of evidence: no region, no entity, and a warning -- never a
    geocoded guess over the top of it (#1668 review, DC-R13)."""
    cfg = {**(entry.data or {}), **(entry.options or {})}
    named = p5min.regions_in_entity_ids(
        [
            cfg.get(CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR),
            cfg.get(CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR),
        ]
    )
    if len(named) > 1:
        _LOGGER.warning(
            "Nimbus P5MIN: the configured AEMO sensors name %s -- more than "
            "one NEM region. Not fetching pre-dispatch until they agree.",
            ", ".join(sorted(named)),
        )
        return None
    if named:
        region = next(iter(named))
    else:
        region, _prefix = sensor_discovery.resolve_geocoded_region_and_prefix(
            hass.states.async_all("sensor")
        )
    return region if region in p5min.NEM_REGIONS else None


def _unzip_text(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.upper().endswith(".CSV"))
        return zf.read(name).decode("utf-8", errors="replace")


class NimbusP5MinForecastSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "AEMO 5-Minute Pre-dispatch"
    _attr_native_unit_of_measurement = (
        "AUD/kWh"  # NEM prices are AUD, whatever the install currency (#1293)
    )
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"forecast", "previous_forecast"})

    def __init__(self, entry, region: str, sw_version: str | None) -> None:
        self._region = region
        self._attr_unique_id = f"{entry.entry_id}_p5min_forecast"
        self.entity_id = "sensor.nimbus_p5min_forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._file: str | None = None
        self._run: dict | None = None
        self._previous: dict | None = None
        self._fetched_at: str | None = None
        self._error: str | None = None
        self._signal_since: datetime | None = None
        self._signal_peak: dict | None = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, POLL)
        )
        self.hass.async_create_background_task(
            self._async_tick(), name="nimbus_p5min_first_fetch"
        )

    def due(self, now) -> bool:
        """Whether the next run can have been published yet."""
        run = (self._run or {}).get("run_datetime")
        if run is None:
            return True
        return now >= datetime.fromisoformat(run) + _NEXT_RUN_DUE

    async def _async_tick(self, _now=None) -> None:
        if not self.due(datetime.now(UTC)):
            return
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(p5min.BASE_URL, timeout=_TIMEOUT) as resp:
                resp.raise_for_status()
                name = p5min.latest_file_name(await resp.text())
            if name is None or name == self._file:
                return
            async with session.get(p5min.BASE_URL + name, timeout=_TIMEOUT) as resp:
                resp.raise_for_status()
                blob = await resp.read()
            text = await self.hass.async_add_executor_job(_unzip_text, blob)
        except Exception as err:  # noqa: BLE001 -- a network fault is not a crash
            self._error = f"{type(err).__name__}: {err}"
            _LOGGER.debug("P5MIN fetch failed: %s", self._error)
            self.async_write_ha_state()
            return
        self.apply(name, p5min.parse_region_solution(text, self._region))

    def apply(self, name: str, run: dict | None) -> None:
        """Take one parsed run. Kept separate from the fetch so it is testable."""
        self._file = name
        self._fetched_at = datetime.now(UTC).isoformat()
        if run is None:
            self._error = f"no {self._region} rows in {name}"
        else:
            self._error = None
            if (
                self._run is not None
                and self._run["run_datetime"] != run["run_datetime"]
            ):
                self._previous = self._run
            self._run = run
            self._update_signal(p5min.published_at(name))
        if self.hass is not None:
            self.async_write_ha_state()

    def _update_signal(self, published: datetime | None) -> None:
        """#1660, shadow only. The age runs from the run that first confirmed
        the episode; a repeat confirmation does not extend it."""
        if self._signal_since is not None and (
            published is None or published - self._signal_since > p5min.SIGNAL_MAX_AGE
        ):
            self._signal_since = self._signal_peak = None
        if self._signal_since is None and p5min.spike_confirmed(
            self._run, self._previous
        ):
            self._signal_since = published
            self._signal_peak = p5min.future_peak(self._run)

    @property
    def available(self) -> bool:
        return self._run is not None

    @property
    def native_value(self) -> float | None:
        rows = (self._run or {}).get("forecast") or []
        return rows[0]["value"] if rows else None

    @property
    def extra_state_attributes(self) -> dict:
        rows = (self._run or {}).get("forecast") or []
        prev = (self._previous or {}).get("forecast") or []
        top, prev_top = p5min.peak(rows), p5min.peak(prev)
        return {
            "region": self._region,
            "run_datetime": (self._run or {}).get("run_datetime"),
            "source_file": self._file,
            "fetched_at": self._fetched_at,
            "last_error": self._error,
            "max_price": top["value"] if top else None,
            "max_price_start": top["start"] if top else None,
            "previous_run_datetime": (self._previous or {}).get("run_datetime"),
            "previous_max_price": prev_top["value"] if prev_top else None,
            "readiness_signal": self._signal_since is not None,
            "readiness_signal_since": (
                self._signal_since.isoformat() if self._signal_since else None
            ),
            "readiness_signal_peak": self._signal_peak,
            "readiness_signal_mode": "shadow",
            "forecast": rows,
            "previous_forecast": prev,
        }
