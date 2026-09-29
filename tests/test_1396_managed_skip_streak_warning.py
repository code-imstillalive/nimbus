"""nimbus issue #1396: a managed entity_id whose publish is skipped every
cycle, forever, must say so once at WARNING.

`ha_post_state()` skips the raw fallback for a managed entity_id with no
registered handler, on the premise that the gap is a setup/unload window
(#312's residual). On devhub that premise was false permanently -- a
`remote_homeassistant` mirror squats the managed id, so no handler ever
registers under it -- and the only trace was a DEBUG line.

The contract pinned here, and the reason for each half:

- the TRANSIENT case the skip exists for stays silent, or the fix would
  reintroduce the #757 noise it is meant to avoid;
- a streak past `_MANAGED_SKIP_WARN_AFTER` warns exactly once per entity_id,
  because the condition is structural and once a minute would be noise;
- recovery is announced and re-arms the warning, so a second, later outage
  is not silently swallowed by the first one's dedup.

Executes `ha_post_state()` against a mocked `_NATIVE_HASS`, the same style
as `test_solver_writer_native_managed_fallback_skip.py`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_shared, solver_writer

_MANAGED = "sensor.nimbus_solver_quality_report"
_OTHER_MANAGED = "sensor.nimbus_offer_curve"
_UNMANAGED = "sensor.nimbus_some_never_migrated_entity"
_N = solver_shared._MANAGED_SKIP_WARN_AFTER


def _reset() -> None:
    solver_writer._NATIVE_HASS = None
    solver_writer._ENTITY_UPDATE_HANDLERS.clear()
    solver_writer._ENTITY_REAL_IDS.clear()
    solver_shared._MANAGED_SKIP_STREAK.clear()
    solver_shared._MANAGED_SKIP_WARNED.clear()


@pytest.fixture
def native(caplog):
    _reset()
    solver_writer._NATIVE_HASS = MagicMock()
    caplog.set_level(logging.INFO, logger=solver_shared._LOGGER.name)
    try:
        yield caplog
    finally:
        _reset()


def _warnings_1396(caplog, level=logging.WARNING):
    return [
        r for r in caplog.records if r.levelno == level and "#1396" in r.getMessage()
    ]


def _post(entity_id, n=1):
    for _ in range(n):
        solver_writer.ha_post_state(entity_id, 1.0, {"k": 1})


def test_both_ids_used_here_really_are_managed():
    assert _MANAGED in solver_shared._NATIVE_MANAGED_ENTITY_IDS
    assert _OTHER_MANAGED in solver_shared._NATIVE_MANAGED_ENTITY_IDS
    assert _UNMANAGED not in solver_shared._NATIVE_MANAGED_ENTITY_IDS


def test_a_transient_window_stays_silent_and_resets(native):
    """The case the skip exists for: a cycle or two without a handler, then
    the entity registers. No warning, and the streak does not carry over."""
    _post(_MANAGED, 2)
    solver_writer.register_entity_handler(_MANAGED, MagicMock())
    _post(_MANAGED)
    assert _warnings_1396(native) == []
    assert _MANAGED not in solver_shared._MANAGED_SKIP_STREAK


def test_no_warning_one_short_of_the_threshold(native):
    _post(_MANAGED, _N - 1)
    assert _warnings_1396(native) == []


def test_a_permanent_skip_warns_exactly_once(native):
    _post(_MANAGED, _N)
    first = _warnings_1396(native)
    assert len(first) == 1
    assert _MANAGED in first[0].getMessage()
    _post(_MANAGED, 5 * _N)
    assert len(_warnings_1396(native)) == 1, "must not repeat every cycle"


def test_the_skip_itself_is_unchanged(native):
    """The warning is additive: a skipped publish still writes nothing."""
    _post(_MANAGED, _N + 1)
    solver_writer._NATIVE_HASS.add_job.assert_not_called()
    solver_writer._NATIVE_HASS.states.async_set.assert_not_called()


def test_recovery_is_announced_and_re_arms_the_warning(native):
    _post(_MANAGED, _N)
    solver_writer.register_entity_handler(_MANAGED, MagicMock())
    _post(_MANAGED)
    assert len(_warnings_1396(native, logging.INFO)) == 1
    solver_writer._ENTITY_UPDATE_HANDLERS.clear()
    _post(_MANAGED, _N)
    assert len(_warnings_1396(native)) == 2, "a second outage must be reported too"


def test_streaks_are_per_entity(native):
    """One squatted id says nothing about another, and must not trip it."""
    _post(_MANAGED, _N)
    _post(_OTHER_MANAGED, _N - 1)
    warned = [r.getMessage() for r in _warnings_1396(native)]
    assert len(warned) == 1 and _MANAGED in warned[0]


def test_unmanaged_ids_are_not_counted(native):
    """They take the raw fallback, so there is no skip to count."""
    _post(_UNMANAGED, _N + 1)
    assert _UNMANAGED not in solver_shared._MANAGED_SKIP_STREAK
    assert _warnings_1396(native) == []
