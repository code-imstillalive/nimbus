"""Real test of NimbusForecastSensor's signal_role attribute (2026-08-23)
-- the mechanism topology-card-v4.js's own _discoverPowerSignalsByRole()
depends on to auto-wire Grid/Battery power, replacing the old Switchboard
wizard fields. See const.py's own comment on CONF_SIGNAL_ROLE for the
full "why explicit, not inferred from naming" reasoning.

Imports and exercises the REAL class (not a reimplementation) against
tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor  # noqa: E402
from custom_components.nimbus_load.const import (  # noqa: E402
    ATTR_SIGNAL_ROLE,
    CONF_LOAD_SENSOR,
    CONF_SIGNAL_ROLE,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_GRID,
    SIGNAL_ROLE_OTHER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)


def _fake_subentry(
    subentry_id: str, subentry_type: str, data: dict, title: str = "Test"
) -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = subentry_type
    s.data = data
    s.title = title
    return s


def _make_sensor(subentry) -> sensor.NimbusForecastSensor:
    coordinator = MagicMock()
    coordinator.data = {}
    return sensor.NimbusForecastSensor(coordinator, subentry, sw_version="1.0.0")


def test_grid_role_is_exposed_as_a_live_attribute():
    subentry = _fake_subentry(
        "ps1",
        SUBENTRY_TYPE_SIGNAL,
        {
            CONF_LOAD_SENSOR: "sensor.logger_meter_total_active_power",
            CONF_SIGNAL_ROLE: SIGNAL_ROLE_GRID,
        },
    )
    s = _make_sensor(subentry)
    assert s.extra_state_attributes[ATTR_SIGNAL_ROLE] == SIGNAL_ROLE_GRID


def test_battery_role_is_exposed_as_a_live_attribute():
    subentry = _fake_subentry(
        "ps2",
        SUBENTRY_TYPE_SIGNAL,
        {
            CONF_LOAD_SENSOR: "sensor.logger_battery_power",
            CONF_SIGNAL_ROLE: SIGNAL_ROLE_BATTERY,
        },
    )
    s = _make_sensor(subentry)
    assert s.extra_state_attributes[ATTR_SIGNAL_ROLE] == SIGNAL_ROLE_BATTERY


def test_a_load_subentry_defaults_to_other_role_harmlessly():
    # Loads never set CONF_SIGNAL_ROLE at all -- confirms the attribute
    # still resolves safely (not a KeyError/crash) rather than assuming
    # every subentry has it.
    subentry = _fake_subentry(
        "load1", SUBENTRY_TYPE_LOAD, {CONF_LOAD_SENSOR: "sensor.cb_pw_l1_power"}
    )
    s = _make_sensor(subentry)
    assert s.extra_state_attributes[ATTR_SIGNAL_ROLE] == SIGNAL_ROLE_OTHER


def test_a_power_signal_with_no_role_set_also_defaults_to_other():
    # A signal created before this feature existed (real, live case for
    # this household's own 4 pre-existing Power Signals).
    subentry = _fake_subentry(
        "ps3",
        SUBENTRY_TYPE_SIGNAL,
        {CONF_LOAD_SENSOR: "sensor.combined_total_dc_power"},
    )
    s = _make_sensor(subentry)
    assert s.extra_state_attributes[ATTR_SIGNAL_ROLE] == SIGNAL_ROLE_OTHER


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
