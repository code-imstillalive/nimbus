"""Real test of solver_runtime._log_dispatch_dry_run() -- phase 1 of the
real-dispatch groundwork (2026-08-27). Confirms this function is genuinely
observe-only: it must never call hass.services.call() (or any other write
path) under any input, only hass.states.get() and logging.

Imports and exercises the REAL function (not a reimplementation) against a
mock hass object, via tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver_runtime import _log_dispatch_dry_run


def _make_hass(switch_state=None, forecast=None):
    hass = MagicMock()
    # No real service-call capability wired up at all -- if the function
    # under test ever reached for it, this would raise AttributeError on
    # a stricter mock, but the real assertion is services.call.assert_
    # not_called() below, which catches it even on a permissive MagicMock.
    hass.services.async_call = MagicMock()
    hass.services.call = MagicMock()

    def _get(entity_id):
        if entity_id == "switch.nimbus_solver_dispatch_dry_run":
            if switch_state is None:
                return None
            state = MagicMock()
            state.state = switch_state
            return state
        if entity_id == "sensor.nimbus_solver_battery_forecast":
            if forecast is None:
                return None
            state = MagicMock()
            state.attributes = {"forecast": forecast}
            return state
        return None

    hass.states.get = MagicMock(side_effect=_get)
    return hass


def test_switch_off_does_nothing():
    hass = _make_hass(switch_state="off", forecast=[{"battery_kw": 5.0}])
    _log_dispatch_dry_run(hass)
    hass.services.async_call.assert_not_called()
    hass.services.call.assert_not_called()


def test_switch_missing_entirely_does_nothing():
    hass = _make_hass(switch_state=None, forecast=[{"battery_kw": 5.0}])
    _log_dispatch_dry_run(hass)
    hass.services.async_call.assert_not_called()


def test_switch_on_but_no_forecast_yet_does_not_crash():
    hass = _make_hass(switch_state="on", forecast=None)
    _log_dispatch_dry_run(hass)  # must not raise
    hass.services.async_call.assert_not_called()


def test_switch_on_with_empty_forecast_list_does_not_crash():
    hass = _make_hass(switch_state="on", forecast=[])
    _log_dispatch_dry_run(hass)
    hass.services.async_call.assert_not_called()


def test_switch_on_with_real_forecast_never_calls_a_service():
    # The real assertion this whole module exists to protect: even with
    # the switch on and a real plan value present, this is pure
    # observation. No write path exists for this to reach yet.
    hass = _make_hass(switch_state="on", forecast=[{"battery_kw": -12.5}])
    _log_dispatch_dry_run(hass)
    hass.services.async_call.assert_not_called()
    hass.services.call.assert_not_called()


def test_malformed_forecast_entry_does_not_crash():
    # A real, if unlikely, shape mismatch -- a period dict missing
    # battery_kw entirely shouldn't blow up the solve cycle that called
    # this (see the function's own try/except-everything docstring).
    hass = _make_hass(switch_state="on", forecast=[{"soc_pct": 50.0}])
    _log_dispatch_dry_run(hass)  # must not raise
    hass.services.async_call.assert_not_called()


def test_hass_states_get_raising_does_not_propagate():
    # Real defensive coverage for the function's own top-of-body
    # try/except -- a genuinely broken hass.states.get() must not turn
    # into a failed solve cycle for what is meant to be a side observation.
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=RuntimeError("boom"))
    _log_dispatch_dry_run(hass)  # must not raise


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
