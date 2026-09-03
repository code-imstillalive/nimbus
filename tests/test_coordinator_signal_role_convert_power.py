"""Real test of NimbusCoordinator._convert_power_for_target (2026-09-03).

Real household bug: a Temperature/Humidity power-signal subentry was
guided into existence using SIGNAL_ROLE_OTHER (the only role that existed
at the time), and coordinator.py unconditionally passed convert_power=True
for its own forecast-target history/lag fetches -- a real, live
"unconvertible unit '°C'/'%' -- treating as kW as-is" WARNING every
coordinator cycle, since Home Assistant's PowerConverter has no notion of
converting a temperature/humidity unit into kW at all. Fixed by making
convert_power depend on the subentry's own signal_role -- this test
exercises the REAL property (not a reimplementation) against a bare
NimbusCoordinator instance, same pattern as test_coordinator_helpers.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_SIGNAL_ROLE,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_HUMIDITY,
    SIGNAL_ROLE_OTHER,
    SIGNAL_ROLE_TEMPERATURE,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _bare_coordinator_with_role(role: str | None) -> NimbusCoordinator:
    c = NimbusCoordinator.__new__(NimbusCoordinator)
    subentry = MagicMock()
    subentry.data = {} if role is None else {CONF_SIGNAL_ROLE: role}
    c.subentry = subentry
    return c


def test_temperature_role_disables_power_conversion():
    c = _bare_coordinator_with_role(SIGNAL_ROLE_TEMPERATURE)
    assert c._signal_role == SIGNAL_ROLE_TEMPERATURE
    assert c._convert_power_for_target is False


def test_humidity_role_disables_power_conversion():
    c = _bare_coordinator_with_role(SIGNAL_ROLE_HUMIDITY)
    assert c._signal_role == SIGNAL_ROLE_HUMIDITY
    assert c._convert_power_for_target is False


def test_battery_role_keeps_power_conversion():
    c = _bare_coordinator_with_role(SIGNAL_ROLE_BATTERY)
    assert c._convert_power_for_target is True


def test_other_role_keeps_power_conversion():
    c = _bare_coordinator_with_role(SIGNAL_ROLE_OTHER)
    assert c._convert_power_for_target is True


def test_no_role_set_at_all_defaults_to_power_conversion():
    # A load subentry, or a power signal created before this feature
    # existed -- must default to the original, pre-fix behaviour.
    c = _bare_coordinator_with_role(None)
    assert c._signal_role == SIGNAL_ROLE_OTHER
    assert c._convert_power_for_target is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
