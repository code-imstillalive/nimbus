"""Real test of solver_runtime._log_dispatch_dry_run() -- phase 1 of the
real-dispatch groundwork (2026-08-27, hardened 2026-08-28). Confirms this
function is genuinely observe-only: it must never call hass.services.call()
(or any other write path) under any input, only hass.states.get(),
logging, and a real recorded sensor push via sw.ha_post_state() (a state
update, not a service call, and itself already proven thread-safe -- see
that function's own docstring in solver_writer.py).

2026-08-28: the original version of this function only logged via
_LOGGER.info() -- confirmed live that this produced zero durable evidence
on a real install, since nimbus_load's default logger level (WARNING)
sits above INFO. Added sw.ha_post_state("sensor.nimbus_solver_dispatch_
dry_run", ...) so the observation survives via HA's own real recorder/
long-term-statistics, independent of logger level. This file's own tests
are extended accordingly -- every switch-on/real-forecast case now also
asserts the sensor push happened with the right value and context
attributes, not just that no service was called.

Imports and exercises the REAL function (not a reimplementation) against a
mock hass object and a mock solver_writer module, via tests/_ha_stubs.py's
stand-in homeassistant.* modules.
"""

from __future__ import annotations

import logging
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


def _make_sw():
    """A bare stand-in for the solver_writer module -- only ha_post_state
    is exercised by the function under test, so only that needs a real
    (mock) attribute; anything else stays absent on purpose so an
    accidental call to some OTHER solver_writer function surfaces as a
    real AttributeError, not a silently-passing MagicMock call.
    """
    sw = MagicMock(spec=["ha_post_state"])
    sw.ha_post_state = MagicMock()
    return sw


def test_switch_off_does_nothing():
    hass = _make_hass(switch_state="off", forecast=[{"battery_kw": 5.0}])
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)
    hass.services.async_call.assert_not_called()
    hass.services.call.assert_not_called()
    sw.ha_post_state.assert_not_called()


def test_switch_missing_entirely_does_nothing():
    hass = _make_hass(switch_state=None, forecast=[{"battery_kw": 5.0}])
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)
    hass.services.async_call.assert_not_called()
    sw.ha_post_state.assert_not_called()


def test_switch_off_logs_at_debug_not_warning(caplog):
    """Nimbus issue #326: a switch that is simply OFF is the ordinary
    steady state for any household not currently evaluating dry-run, and
    this function runs once per ~5-minute solve cycle. Warning about it
    every cycle drowns real signal -- measured live at 11 lines in a
    16-minute window, part of a 41-of-100-line recurring-WARNING share.

    Asserts the LEVEL, not just that something was logged: the whole
    point of the issue is that the message still exists for anyone who
    raises the logger to DEBUG, it just stops shouting at everyone else.
    """
    hass = _make_hass(switch_state="off", forecast=[{"battery_kw": 5.0}])
    sw = _make_sw()
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.nimbus_load.solver_runtime"
    ):
        _log_dispatch_dry_run(hass, sw)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], (
        f"switch-off must not warn, got: {[r.message for r in warnings]}"
    )
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("skipping this cycle" in r.getMessage() for r in debugs), (
        "the message must still be emitted at DEBUG, not dropped entirely"
    )


def test_switch_missing_entity_still_warns(caplog):
    """The other half of #326: a MISSING switch entity is an install-
    integrity problem, not a user preference, so it keeps WARNING. This
    is the distinction the issue explicitly asked to preserve -- a blanket
    demotion of the whole branch would have silenced it too.
    """
    hass = _make_hass(switch_state=None, forecast=[{"battery_kw": 5.0}])
    sw = _make_sw()
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.nimbus_load.solver_runtime"
    ):
        _log_dispatch_dry_run(hass, sw)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"missing entity must warn exactly once, got {len(warnings)}"
    )
    assert "entity is missing" in warnings[0].getMessage()


def test_switch_on_but_no_forecast_yet_does_not_crash():
    hass = _make_hass(switch_state="on", forecast=None)
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)  # must not raise
    hass.services.async_call.assert_not_called()
    sw.ha_post_state.assert_not_called()


def test_switch_on_with_empty_forecast_list_does_not_crash():
    hass = _make_hass(switch_state="on", forecast=[])
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)
    hass.services.async_call.assert_not_called()
    sw.ha_post_state.assert_not_called()


def test_switch_on_with_real_forecast_never_calls_a_service():
    # The real assertion this whole module exists to protect: even with
    # the switch on and a real plan value present, this is pure
    # observation. No write/service path exists for this to reach yet --
    # only a state PUSH to a plain diagnostic sensor.
    hass = _make_hass(switch_state="on", forecast=[{"battery_kw": -12.5}])
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)
    hass.services.async_call.assert_not_called()
    hass.services.call.assert_not_called()


def test_switch_on_with_real_forecast_records_a_durable_sensor_update():
    """The actual 2026-08-28 fix under test: a real forecast period with
    the switch on must produce a real sw.ha_post_state() call for
    sensor.nimbus_solver_dispatch_dry_run, carrying the rounded
    battery_kw as state and the real per-period context as attributes --
    this is what makes the dry run durable/reviewable via HA's own
    History and long-term statistics, unlike the original log-line-only
    version.
    """
    period = {
        "battery_kw": -12.4567,
        "soc_pct": 41.2,
        "grid_import_kw": 3.5,
        "grid_export_kw": 0.0,
        "import_price": 0.154,
        "export_price": 0.003,
        "time": "2026-08-28T10:20:00+10:00",
    }
    hass = _make_hass(switch_state="on", forecast=[period])
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)

    sw.ha_post_state.assert_called_once()
    args, _kwargs = sw.ha_post_state.call_args
    entity_id, state, attributes = args
    assert entity_id == "sensor.nimbus_solver_dispatch_dry_run"
    assert state == round(-12.4567, 3)
    assert attributes["soc_pct"] == 41.2
    assert attributes["grid_import_kw"] == 3.5
    assert attributes["grid_export_kw"] == 0.0
    assert attributes["import_price"] == 0.154
    assert attributes["export_price"] == 0.003
    assert attributes["period_time"] == "2026-08-28T10:20:00+10:00"
    assert attributes["dry_run_enabled"] is True


def test_malformed_forecast_entry_does_not_crash():
    # A real, if unlikely, shape mismatch -- a period dict missing
    # battery_kw entirely shouldn't blow up the solve cycle that called
    # this (see the function's own try/except-everything docstring).
    hass = _make_hass(switch_state="on", forecast=[{"soc_pct": 50.0}])
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)  # must not raise
    hass.services.async_call.assert_not_called()
    sw.ha_post_state.assert_not_called()


def test_hass_states_get_raising_does_not_propagate():
    # Real defensive coverage for the function's own top-of-body
    # try/except -- a genuinely broken hass.states.get() must not turn
    # into a failed solve cycle for what is meant to be a side observation.
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=RuntimeError("boom"))
    sw = _make_sw()
    _log_dispatch_dry_run(hass, sw)  # must not raise
    sw.ha_post_state.assert_not_called()


def test_sw_ha_post_state_raising_does_not_propagate(caplog):
    # The sensor push itself is new (2026-08-28) -- confirm a broken
    # ha_post_state() (e.g. a torn-down entity mid-reload) is caught by
    # the same top-of-body try/except as everything else in this
    # function, not a new, separate failure mode this fix could
    # introduce into the real solve cycle.
    #
    # nimbus issue #360 (Mark Purcell, codebase review): "must not raise"
    # alone doesn't prove ha_post_state was ever genuinely reached and
    # called -- a future early return right before it would pass this
    # test just as well. Asserting the call happened, and that the
    # except clause's own log line fired, closes that gap.
    hass = _make_hass(switch_state="on", forecast=[{"battery_kw": 1.0}])
    sw = _make_sw()
    sw.ha_post_state.side_effect = RuntimeError("boom")
    with caplog.at_level("ERROR"):
        _log_dispatch_dry_run(hass, sw)  # must not raise
    sw.ha_post_state.assert_called_once()
    assert "logging failed, ignoring" in caplog.text
