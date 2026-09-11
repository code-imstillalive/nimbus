#!/usr/bin/env python3
"""nimbus issue #752 (Mark Purcell) -- extends #741/#750's own pseudo-HWS
pattern (see the sibling mqtt_pseudo_water_heater.py in this same folder)
to a full pseudo-device fleet for devhub: pool heat pump, HVAC, pool
pump, bidirectional EV, dishwasher, washing machine, dryer. Lets devhub
exercise the whole Controllable Load AND multi-battery-participant
config surface without any of it touching a real household appliance --
directly answers the standing asks that had sat open on #482 (a real
price-gated load to verify `profit_horizon` against) and #563 (a real
second live multi-battery-participant, the gap flagged by that issue's
own last two comments), both waiting on exactly this.

One shared daemon process (not one script per device, per #752's own
"implementation detail, not blocking" framing) -- simpler to deploy and
keeps one real MQTT connection instead of seven.

## What this publishes

**Five switch-domain Controllable Loads** (matching
`_sample_load_run_state()`'s own switch+companion-power-sensor pairing,
the same mechanism the real HWS is dispatched through elsewhere in this
household): pool heat pump (5 kW), pool pump (1.5 kW), dishwasher
(2 kW), washing machine (1.5 kW), dryer (2 kW). Each is a real MQTT
`switch` entity (`switch.turn_on`/`switch.turn_off` genuinely flips its
own published state) plus a companion power sensor reporting rated
power while on, 0 while off -- no independent duty-cycle simulation
needed, since Nimbus's own dispatch (once a `controllable_load`
subentry is pointed at each) is what decides when they run. Configured
as `deferrable` (matching Mark's own reasoning: real washing
machines/dishwashers/dryers can't be paused mid-cycle on real hardware,
which points at deferrable/semi-continuous over sheddable, same
shape as the HWS).

**One background HVAC load** (`sensor.nimbus_test_hvac_power`, no
switch -- #481, thermal-state loads, isn't built yet, so this can't be
wired into Nimbus's own scheduling; it exists as a realistic
uncontrolled load for the Forecaster to learn from). Power tracks a
simple simulated outdoor temperature (a daily sine wave, published
alongside as `sensor.nimbus_test_outdoor_temp` for transparency):
2 kW whenever that simulated temperature exceeds 22 C, 0 otherwise.

**One bidirectional EV battery_participant** (`sensor.nimbus_test_ev_soc`,
`sensor.nimbus_test_ev_power`, `binary_sensor.nimbus_test_ev_available`):
60 kWh capacity, 12 kW max charge/discharge, drives away on a daily
08:00-17:00 (local) simulated commute draining ~11 kWh (roughly 50 km
at a realistic ~4.5 km/kWh), `available` False for that whole window
(the same whole-horizon gating `battery_participant_available_entity`
already reads elsewhere), power pinned to 0 and SoC held flat while
home and idle -- **honest limitation, not an oversight**: this
project's own `battery_participant` config surface is currently a
read-only monitoring model (SoC/power/available sensors feed the
Solver's own plan; there is no live command-write path back to a real
EV yet), so this fixture validates the Solver's own plan-building
against a genuine second live multi-battery-participant signal shape
(including a real departure-deadline target, see below), not
closed-loop EV charge control -- there's nothing to close the loop
with yet.

## Usage

    pip install paho-mqtt
    MQTT_HOST=core-mosquitto MQTT_USERNAME=... MQTT_PASSWORD=... \\
        python3 mqtt_pseudo_device_fleet.py

Runs forever (a daemon, not a one-shot). Same deployment posture as
`mqtt_pseudo_water_heater.py` -- see that file's own docstring, and
this repo's own `docs/real-world-integration/README.md`, for the
`init_commands` pattern that survives an add-on/host restart. State
resets to sensible starting values on restart, not persisted -- these
are test fixtures, not real appliances.

## Wiring into Nimbus once running

Once entities appear via MQTT Discovery:
- The five switch-domain devices: one `controllable_load` subentry
  each (kind=deferrable, Device entity = the switch, Power sensor =
  the companion sensor), same fields as the real HWS.
- HVAC: a plain `Load` subentry pointed at
  `sensor.nimbus_test_hvac_power` -- a normal forecastable load, no
  Controllable Load config at all.
- EV: one `battery_participant` subentry -- SoC sensor
  `sensor.nimbus_test_ev_soc`, Power sensor `sensor.nimbus_test_ev_power`
  (`power_positive_is_charge=True`, matching this script's own
  convention), Available entity
  `binary_sensor.nimbus_test_ev_available`, and (to exercise the real
  departure-deadline mechanism, not just passive monitoring)
  departure_hour=8, must_have_soc_by_departure_percent=80.

## What it is NOT

Same posture as `mqtt_pseudo_water_heater.py`: not wired into Nimbus's
own test suite (needs a live MQTT broker and hass instance, neither
exists in CI) -- exists for the gap between "unit tests prove the LP
mechanics are sound in isolation" and "does a real live multi-device,
multi-battery-participant solve actually behave sensibly," on
infrastructure built specifically to absorb that risk.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_LOG = logging.getLogger("nimbus_pseudo_fleet")

# ---- configuration (env vars, all optional) --------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "30"))
# Real households run on local wall-clock time, not UTC -- these
# simulations (the EV's own commute window, HVAC's own diurnal cycle)
# need a real local-time reference to look realistic. Defaults to the
# reference household's own zone; override for a different install.
LOCAL_UTC_OFFSET_HOURS = float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "10"))

_NODE_ID = "nimbus_test_fleet"


def _local_now() -> datetime:
    return datetime.now(UTC) + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)


# ---- shared MQTT publish helpers -------------------------------------------


class _Fleet:
    """Owns the single MQTT client every device publishes/subscribes
    through, and the lock guarding all simulated state -- the network
    thread (on_message) and the simulation-tick thread both touch this,
    same reasoning as mqtt_pseudo_water_heater.py's own state class."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.client = mqtt.Client(client_id=f"nimbus-pseudo-fleet-{_NODE_ID}")
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        # switch entity_id_suffix -> bool (True = on)
        self.switch_state: dict[str, bool] = {}
        # command topic -> switch entity_id_suffix, resolved once at
        # discovery-publish time so on_message doesn't need to re-derive
        # it from the topic string on every command.
        self._command_topics: dict[str, str] = {}
        self.ev_soc_pct = 62.0

    def _topic(self, kind: str, suffix: str, sub: str) -> str:
        return f"nimbus_test_fleet/{kind}/{suffix}/{sub}"

    def _on_connect(self, client, userdata, flags, rc, *_):
        if rc != 0:
            _LOG.error("MQTT connect failed, rc=%s", rc)
            return
        _LOG.info("MQTT connected, publishing discovery configs")
        for suffix, name, rated_kw in SWITCH_LOADS:
            self._publish_switch_discovery(suffix, name, rated_kw)
        self._publish_hvac_discovery()
        self._publish_ev_discovery()
        for topic in self._command_topics:
            client.subscribe(topic, qos=1)

    def _on_message(self, client, userdata, msg):
        suffix = self._command_topics.get(msg.topic)
        if suffix is None:
            return
        payload = msg.payload.decode("utf-8", "replace").strip().upper()
        on = payload == "ON"
        with self._lock:
            self.switch_state[suffix] = on
        _LOG.info("switch %s -> %s", suffix, "ON" if on else "OFF")
        self._publish_switch_state(suffix)

    # -- switch-domain Controllable Loads ------------------------------

    def _publish_switch_discovery(
        self, suffix: str, name: str, rated_kw: float
    ) -> None:
        state_topic = self._topic("switch", suffix, "state")
        command_topic = self._topic("switch", suffix, "set")
        power_state_topic = self._topic("switch", suffix, "power")
        avail_topic = self._topic("switch", suffix, "availability")
        self._command_topics[command_topic] = suffix
        switch_payload = {
            "name": name,
            "unique_id": f"{_NODE_ID}_{suffix}",
            "state_topic": state_topic,
            "command_topic": command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": avail_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [f"{_NODE_ID}_{suffix}"],
                "name": name,
                "manufacturer": "Nimbus",
                "model": f"Pseudo {name} (test fixture, nimbus issue #752)",
            },
        }
        power_payload = {
            "name": f"{name} Power",
            "unique_id": f"{_NODE_ID}_{suffix}_power",
            "state_topic": power_state_topic,
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "availability_topic": avail_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": switch_payload["device"],
        }
        self.client.publish(
            f"{DISCOVERY_PREFIX}/switch/{_NODE_ID}_{suffix}/config",
            json.dumps(switch_payload),
            qos=1,
            retain=True,
        )
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{_NODE_ID}_{suffix}_power/config",
            json.dumps(power_payload),
            qos=1,
            retain=True,
        )
        self.client.publish(avail_topic, "online", qos=1, retain=True)
        with self._lock:
            self.switch_state.setdefault(suffix, False)
        self._publish_switch_state(suffix)
        self._publish_switch_power(suffix, rated_kw)

    def _publish_switch_state(self, suffix: str) -> None:
        with self._lock:
            on = self.switch_state.get(suffix, False)
        self.client.publish(
            self._topic("switch", suffix, "state"),
            "ON" if on else "OFF",
            qos=1,
            retain=True,
        )

    def _publish_switch_power(self, suffix: str, rated_kw: float) -> None:
        with self._lock:
            on = self.switch_state.get(suffix, False)
        power = rated_kw if on else 0.0
        self.client.publish(
            self._topic("switch", suffix, "power"),
            f"{power:.3f}",
            qos=1,
            retain=True,
        )

    # -- HVAC (background, uncontrolled) --------------------------------

    def _publish_hvac_discovery(self) -> None:
        power_topic = self._topic("hvac", "main", "power")
        temp_topic = self._topic("hvac", "main", "outdoor_temp")
        power_payload = {
            "name": "Nimbus Test HVAC Power",
            "unique_id": f"{_NODE_ID}_hvac_power",
            "state_topic": power_topic,
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "device": {
                "identifiers": [f"{_NODE_ID}_hvac"],
                "name": "Nimbus Test HVAC",
                "manufacturer": "Nimbus",
                "model": "Pseudo HVAC (test fixture, nimbus issue #752)",
            },
        }
        temp_payload = {
            "name": "Nimbus Test Outdoor Temp",
            "unique_id": f"{_NODE_ID}_hvac_outdoor_temp",
            "state_topic": temp_topic,
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "device": power_payload["device"],
        }
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{_NODE_ID}_hvac_power/config",
            json.dumps(power_payload),
            qos=1,
            retain=True,
        )
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{_NODE_ID}_hvac_outdoor_temp/config",
            json.dumps(temp_payload),
            qos=1,
            retain=True,
        )

    def publish_hvac_tick(self) -> None:
        now = _local_now()
        hour = now.hour + now.minute / 60.0
        # Diurnal sine: trough ~2am (12C), peak ~2pm (24C) -- a plain,
        # honest simulated shape, not real weather data.
        outdoor_temp = 18.0 + 6.0 * math.sin(2 * math.pi * (hour - 8) / 24)
        power = HVAC_RATED_KW if outdoor_temp > HVAC_TRIGGER_C else 0.0
        self.client.publish(
            self._topic("hvac", "main", "outdoor_temp"),
            f"{outdoor_temp:.2f}",
            qos=1,
            retain=True,
        )
        self.client.publish(
            self._topic("hvac", "main", "power"), f"{power:.3f}", qos=1, retain=True
        )

    # -- Bidirectional EV battery_participant ----------------------------

    def _publish_ev_discovery(self) -> None:
        soc_topic = self._topic("ev", "main", "soc")
        power_topic = self._topic("ev", "main", "power")
        avail_topic = self._topic("ev", "main", "available")
        device = {
            "identifiers": [f"{_NODE_ID}_ev"],
            "name": "Nimbus Test EV",
            "manufacturer": "Nimbus",
            "model": "Pseudo bidirectional EV (test fixture, nimbus issue #752)",
        }
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{_NODE_ID}_ev_soc/config",
            json.dumps(
                {
                    "name": "Nimbus Test EV SoC",
                    "unique_id": f"{_NODE_ID}_ev_soc",
                    "state_topic": soc_topic,
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "state_class": "measurement",
                    "device": device,
                }
            ),
            qos=1,
            retain=True,
        )
        self.client.publish(
            f"{DISCOVERY_PREFIX}/sensor/{_NODE_ID}_ev_power/config",
            json.dumps(
                {
                    "name": "Nimbus Test EV Power",
                    "unique_id": f"{_NODE_ID}_ev_power",
                    "state_topic": power_topic,
                    "unit_of_measurement": "kW",
                    "device_class": "power",
                    "state_class": "measurement",
                    "device": device,
                }
            ),
            qos=1,
            retain=True,
        )
        self.client.publish(
            f"{DISCOVERY_PREFIX}/binary_sensor/{_NODE_ID}_ev_available/config",
            json.dumps(
                {
                    "name": "Nimbus Test EV Available",
                    "unique_id": f"{_NODE_ID}_ev_available",
                    "state_topic": avail_topic,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device": device,
                }
            ),
            qos=1,
            retain=True,
        )

    def publish_ev_tick(self, elapsed_hours: float) -> None:
        now = _local_now()
        away = EV_AWAY_START_HOUR <= now.hour < EV_AWAY_END_HOUR
        with self._lock:
            if away:
                drain_kwh = EV_DAILY_DRIVE_KWH / (EV_AWAY_END_HOUR - EV_AWAY_START_HOUR)
                self.ev_soc_pct = max(
                    5.0,
                    self.ev_soc_pct
                    - (drain_kwh / EV_CAPACITY_KWH) * 100.0 * elapsed_hours,
                )
                power_kw = 0.0
            else:
                # Home and idle -- no live command-write path exists yet
                # for battery_participant (see this file's own module
                # docstring), so power stays honestly at 0 rather than
                # simulating a charge/discharge Nimbus never actually
                # commanded.
                power_kw = 0.0
            soc = self.ev_soc_pct
        self.client.publish(
            self._topic("ev", "main", "soc"), f"{soc:.2f}", qos=1, retain=True
        )
        self.client.publish(
            self._topic("ev", "main", "power"), f"{power_kw:.3f}", qos=1, retain=True
        )
        self.client.publish(
            self._topic("ev", "main", "available"),
            "OFF" if away else "ON",
            qos=1,
            retain=True,
        )


# Mark's own requested fleet, per #752's own table -- name shown in HA,
# entity-id suffix, rated power.
SWITCH_LOADS = [
    ("pool_heat_pump", "Nimbus Test Pool Heat Pump", 5.0),
    ("pool_pump", "Nimbus Test Pool Pump", 1.5),
    ("dishwasher", "Nimbus Test Dishwasher", 2.0),
    ("washing_machine", "Nimbus Test Washing Machine", 1.5),
    ("dryer", "Nimbus Test Dryer", 2.0),
]
HVAC_RATED_KW = 2.0
HVAC_TRIGGER_C = 22.0
EV_CAPACITY_KWH = 60.0
EV_AWAY_START_HOUR = 8
EV_AWAY_END_HOUR = 17
# ~50 km/day at a realistic ~4.5 km/kWh -- Mark's own requested duty.
EV_DAILY_DRIVE_KWH = 11.0


def main() -> None:
    fleet = _Fleet()
    _LOG.info("connecting to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
    fleet.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    fleet.client.loop_start()

    last_tick = time.monotonic()
    try:
        while True:
            time.sleep(TICK_SECONDS)
            now = time.monotonic()
            elapsed_hours = (now - last_tick) / 3600.0
            last_tick = now
            for suffix, _name, rated_kw in SWITCH_LOADS:
                fleet._publish_switch_power(suffix, rated_kw)
            fleet.publish_hvac_tick()
            fleet.publish_ev_tick(elapsed_hours)
            _LOG.info("tick complete")
    except KeyboardInterrupt:
        pass
    finally:
        for suffix, _name, _rated_kw in SWITCH_LOADS:
            fleet.client.publish(
                fleet._topic("switch", suffix, "availability"),
                "offline",
                qos=1,
                retain=True,
            )
        fleet.client.loop_stop()
        fleet.client.disconnect()


if __name__ == "__main__":
    main()
