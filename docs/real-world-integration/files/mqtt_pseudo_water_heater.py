#!/usr/bin/env python3
"""Nimbus issue #741 (Mark Purcell) -- a pseudo `water_heater` device for
DAILY, ongoing testing on devhub: "are HWS controllable loads actually
activated, or just repeatedly scheduled and deferred?"

The unit tests in `tests/test_solver_writer_controllable_loads.py`
(`TestFlipFloppingRawDecisionNeverActivatesHws`) already prove
`apply_commanded_state_guard()`'s own debounce logic is sound in
isolation -- a synthetic on/off/on/off sequence never falsely
activates, and a genuinely stable decision does. What those tests
CANNOT exercise is the real, live coordinator: the actual 2-minute
solve cadence, the actual LP re-solving against real (changing) prices
and a real household load/solar forecast, and whether the LP's own
period-0 raw decision oscillates in practice the way it did the real
morning #741 documents. That needs a real device Nimbus can be pointed
at continuously, without risking the household's own real WWK302 tank
(and, per #731/#741's own findings, its own real thermal floor and
cold-shower risk) as the guinea pig.

This script publishes a real `water_heater` entity into Home Assistant
via MQTT Discovery -- shaped exactly like the real household's own
device (min_temp=45, max_temp=65, eco/performance modes,
current_temperature attribute), backed by a simple but real thermal
simulation (heats in performance mode, decays and self-defends its own
floor in eco mode, the same eco-mode "minimum thermal cut-in" behaviour
#731 found live on the real device). Point a `controllable_load`
subentry's own Device entity field at the resulting
`water_heater.nimbus_test_hws` and Nimbus will genuinely try to
schedule and dispatch it every solve, on devhub, every day -- with
nothing worse than a fake number if it goes wrong.

## Usage

    pip install paho-mqtt
    MQTT_HOST=core-mosquitto MQTT_USERNAME=... MQTT_PASSWORD=... \\
        python3 mqtt_pseudo_water_heater.py

Runs forever (a daemon, not a one-shot). Meant to run as a long-lived
process on devhub itself (a systemd unit, a `docker run -d`, or
alongside the Home Assistant container) -- see this folder's own
README for how the other reference scripts here are deployed. Safe to
restart at any time; state resets to a mid-range starting temperature,
not persisted across restarts (this is a test fixture, not a real tank
-- persistence was a deliberately skipped feature, not an oversight).

## What it is NOT

Not a replacement for a real device integration test, and not wired
into Nimbus's own test suite (`tests/`) -- it needs a live MQTT broker
and a live `hass` instance, neither of which exist in CI. It exists
specifically for the gap between "unit tests prove the guard logic is
sound" and "does the real system actually activate a real device the
way a household would experience it" -- run it, configure a
`controllable_load` subentry against it, and watch
`sensor.nimbus_nimbus_test_hws_status` /
`sensor.nimbus_nimbus_test_hws_next_start` over a real day or two.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_LOG = logging.getLogger("nimbus_pseudo_hws")

# ---- configuration (env vars, all optional) --------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
DEVICE_ID = os.environ.get("DEVICE_ID", "nimbus_test_hws")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Nimbus Test HWS")
DISCOVERY_PREFIX = os.environ.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "30"))

# Real values confirmed live against the household's own WWK302
# (custom_components/nimbus_load's own #731/#741 investigation) --
# deliberately matched, not invented, so a controllable_load subentry
# configured identically to the real one behaves the same way here.
MIN_TEMP_C = 45.0
MAX_TEMP_C = 65.0
START_TEMP_C = 50.0

# Thermal simulation rates, derived from real observed behaviour this
# same investigation measured (worklog: ~7C rise over ~1.5h of real
# "performance" mode activation; the real eco-mode floor-defense event
# recovered ~6C in about an hour once triggered). Deliberately simple
# (linear, not a real heat-transfer model) -- #481 (thermal-kind loads,
# a genuine LP heat-transfer model) is the real, tracked, NOT-built
# feature this script is not trying to preempt; this is a test fixture,
# not a simulation of record.
PERFORMANCE_HEATING_C_PER_HOUR = 4.6
ECO_IDLE_DECAY_C_PER_HOUR = 1.0
ECO_FLOOR_DEFENSE_C_PER_HOUR = 2.0  # engages only once temp <= MIN_TEMP_C

_BASE_TOPIC = f"nimbus_test/{DEVICE_ID}"
_MODE_COMMAND_TOPIC = f"{_BASE_TOPIC}/mode/set"
_MODE_STATE_TOPIC = f"{_BASE_TOPIC}/mode/state"
_TEMP_STATE_TOPIC = f"{_BASE_TOPIC}/temperature/state"
_AVAILABILITY_TOPIC = f"{_BASE_TOPIC}/availability"
_DISCOVERY_TOPIC = f"{DISCOVERY_PREFIX}/water_heater/{DEVICE_ID}/config"


class _PseudoWaterHeaterState:
    """Guarded by `_lock` -- the MQTT network thread (on_message) and the
    simulation-tick thread both touch this, and paho-mqtt calls
    on_message from its own network loop thread, not the caller's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode = "eco"
        self.current_temperature = START_TEMP_C

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode

    def tick(self, elapsed_hours: float) -> None:
        with self._lock:
            if self.mode == "performance":
                self.current_temperature += (
                    PERFORMANCE_HEATING_C_PER_HOUR * elapsed_hours
                )
            elif self.current_temperature <= MIN_TEMP_C:
                # nimbus issue #731's own live finding: eco mode is not
                # really "off" -- the real device defends its own floor
                # regardless of what Nimbus last commanded. Reproduced
                # here deliberately, not a bug in this script.
                self.current_temperature += ECO_FLOOR_DEFENSE_C_PER_HOUR * elapsed_hours
            else:
                self.current_temperature -= ECO_IDLE_DECAY_C_PER_HOUR * elapsed_hours
            self.current_temperature = max(
                MIN_TEMP_C, min(MAX_TEMP_C, self.current_temperature)
            )

    def snapshot(self) -> tuple[str, float]:
        with self._lock:
            return self.mode, self.current_temperature


def _discovery_payload() -> dict:
    """HA MQTT water_heater discovery config -- mirrors the real
    household device's own real attributes (min_temp/max_temp/
    operation_list) confirmed live in #731/#741, not invented values."""
    return {
        "name": DEVICE_NAME,
        "unique_id": DEVICE_ID,
        "modes": ["eco", "performance"],
        "mode_command_topic": _MODE_COMMAND_TOPIC,
        "mode_state_topic": _MODE_STATE_TOPIC,
        "current_temperature_topic": _TEMP_STATE_TOPIC,
        "min_temp": MIN_TEMP_C,
        "max_temp": MAX_TEMP_C,
        "temperature_unit": "C",
        "availability_topic": _AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [DEVICE_ID],
            "name": DEVICE_NAME,
            "manufacturer": "Nimbus",
            "model": "Pseudo HWS (test fixture, nimbus issue #741)",
        },
    }


def _on_connect(client: mqtt.Client, state: _PseudoWaterHeaterState, flags, rc, *_):
    if rc != 0:
        _LOG.error("MQTT connect failed, rc=%s", rc)
        return
    _LOG.info("MQTT connected, publishing discovery config to %s", _DISCOVERY_TOPIC)
    client.publish(
        _DISCOVERY_TOPIC, json.dumps(_discovery_payload()), qos=1, retain=True
    )
    client.publish(_AVAILABILITY_TOPIC, "online", qos=1, retain=True)
    client.subscribe(_MODE_COMMAND_TOPIC, qos=1)
    mode, temp = state.snapshot()
    client.publish(_MODE_STATE_TOPIC, mode, qos=1, retain=True)
    client.publish(_TEMP_STATE_TOPIC, f"{temp:.2f}", qos=1, retain=True)


def _on_message(client: mqtt.Client, state: _PseudoWaterHeaterState, msg):
    if msg.topic != _MODE_COMMAND_TOPIC:
        return
    mode = msg.payload.decode("utf-8", "replace").strip()
    if mode not in ("eco", "performance"):
        _LOG.warning("ignoring unrecognized mode command: %r", mode)
        return
    # Real dispatch_commanded_state() call landed here -- this is the
    # exact moment #741's own real-world question gets answered live:
    # did Nimbus actually send this, and how often?
    _LOG.info("received set_operation_mode(%s)", mode)
    state.set_mode(mode)
    client.publish(_MODE_STATE_TOPIC, mode, qos=1, retain=True)


def main() -> None:
    state = _PseudoWaterHeaterState()
    client = mqtt.Client(client_id=f"nimbus-pseudo-hws-{DEVICE_ID}")
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.will_set(_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
    client.on_connect = lambda c, _u, flags, rc, *a: _on_connect(
        c, state, flags, rc, *a
    )
    client.on_message = lambda c, _u, msg: _on_message(c, state, msg)

    _LOG.info(
        "connecting to MQTT broker %s:%s as device_id=%s",
        MQTT_HOST,
        MQTT_PORT,
        DEVICE_ID,
    )
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    last_tick = time.monotonic()
    try:
        while True:
            time.sleep(TICK_SECONDS)
            now = time.monotonic()
            elapsed_hours = (now - last_tick) / 3600.0
            last_tick = now
            state.tick(elapsed_hours)
            mode, temp = state.snapshot()
            client.publish(_TEMP_STATE_TOPIC, f"{temp:.2f}", qos=1, retain=True)
            _LOG.info("tick: mode=%s current_temperature=%.2fC", mode, temp)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
