"""Real regression guard for nimbus issue #312's residual (root-caused
2026-09-01 -- see __init__.py's own async_unload_entry() docstring and
solver_writer.py's own _NATIVE_MANAGED_ENTITY_IDS comment for the full
incident).

`ha_post_state()`'s native-mode branch has always had a raw
`hass.states.async_set()` fallback for whenever no SensorEntity has
registered itself as an entity_id's push handler yet -- correct and
necessary for the standalone/cron/addon deployment (which never has a
real entity object at all), but also, unintentionally, reachable for a
handful of entity_ids that DO eventually get a real handler (sensor.py's
own hub-level push sensors) during the narrow window before/after their
own (re-)registration. A raw write in THAT window creates a
non-`RESTORED` ghost state that then collides with the real entity's
own registration a moment later -- the exact, live-confirmed mechanism
behind "Platform nimbus_load does not generate unique IDs."

Fix: for the closed, enumerable set of entity_ids sensor.py's own
async_setup_entry() DOES eventually call register_entity_handler() for
(_NATIVE_MANAGED_ENTITY_IDS), a missing handler now means "skip this
cycle's update, the real entity will publish fresh data once it
(re-)registers" instead of writing a raw ghost state. Any OTHER
entity_id (not in that set) keeps today's exact fallback behaviour,
unchanged -- this is additive, not a general fallback removal.

This test actually EXECUTES ha_post_state() against a mocked _NATIVE_HASS
(confirmed importable/callable under tests/_ha_stubs.py's stand-in
homeassistant.* modules plus tests/_solver_path.py's sys.path setup for
the pure-Python solver/ml packages this module also imports) rather than
using this project's usual source-inspection style for solver_writer.py
tests -- a real behavioural assertion is possible here and is strictly
stronger evidence than a source-text match for a fix that's entirely
about runtime branching behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

_MANAGED_ENTITY_ID = "sensor.nimbus_solver_quality_report"
_UNMANAGED_ENTITY_ID = "sensor.nimbus_some_never_migrated_entity"


def _reset_module_state() -> None:
    solver_writer._NATIVE_HASS = None
    solver_writer._ENTITY_UPDATE_HANDLERS.clear()
    solver_writer._ENTITY_REAL_IDS.clear()


def test_managed_entity_id_is_actually_in_the_frozenset():
    # Guards against the set drifting out of sync with sensor.py's own
    # register_entity_handler() call sites -- this specific entity_id is
    # the one directly observed colliding live on devhub.
    assert _MANAGED_ENTITY_ID in solver_writer._NATIVE_MANAGED_ENTITY_IDS


def test_managed_entity_with_no_handler_skips_the_raw_fallback():
    _reset_module_state()
    mock_hass = MagicMock()
    solver_writer._NATIVE_HASS = mock_hass
    try:
        solver_writer.ha_post_state(_MANAGED_ENTITY_ID, 86.11, {"epr": 0.8611})
        mock_hass.add_job.assert_not_called()
        mock_hass.states.async_set.assert_not_called()
    finally:
        _reset_module_state()


def test_managed_entity_with_a_registered_handler_still_dispatches_normally():
    _reset_module_state()
    mock_hass = MagicMock()
    solver_writer._NATIVE_HASS = mock_hass
    handler = MagicMock()
    solver_writer.register_entity_handler(_MANAGED_ENTITY_ID, handler)
    try:
        solver_writer.ha_post_state(_MANAGED_ENTITY_ID, 86.11, {"epr": 0.8611})
        mock_hass.add_job.assert_called_once()
        # The dispatched job must wrap the registered handler, not the
        # raw states.async_set fallback.
        dispatched = mock_hass.add_job.call_args[0][0]
        assert dispatched.func is handler
    finally:
        _reset_module_state()


def test_unmanaged_entity_id_still_uses_the_raw_fallback_unchanged():
    # Behaviour for anything NOT in _NATIVE_MANAGED_ENTITY_IDS (the
    # standalone/cron/addon deployment's own entities, or a genuinely
    # not-yet-migrated one) must be completely untouched by this fix.
    _reset_module_state()
    mock_hass = MagicMock()
    solver_writer._NATIVE_HASS = mock_hass
    try:
        solver_writer.ha_post_state(_UNMANAGED_ENTITY_ID, 42, {"unit": "kW"})
        mock_hass.add_job.assert_called_once()
        dispatched = mock_hass.add_job.call_args[0][0]
        assert dispatched.func is mock_hass.states.async_set
        assert dispatched.args == (_UNMANAGED_ENTITY_ID, 42, {"unit": "kW"})
    finally:
        _reset_module_state()


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
