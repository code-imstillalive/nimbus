"""nimbus #1634 (Mark Purcell, 10 Oct): on v0.94.445 his
`sensor.nimbus_flex_telemetry` stayed `unknown` from the restart onward, with
no `record`, no #1635 `no_record` reason, and no WARNING.

The mechanism, pinned here end to end:

- native `ha_post_state()` deliberately DROPS a post for a managed entity_id
  whose entity has not registered its handler yet (#312: mid-setup), on the
  premise that "the next cycle will publish";
- `_post_no_record_reason()` remembered the reason as posted whether or not
  it was delivered, and skips every later cycle while the reason is unchanged.

So the first solve after a restart, if it ran before the entity registered,
lost the only post the sensor would ever get. A record is re-posted every
interval and never had this problem; an unchanging reason (e.g. region not
resolved) stuck for good. `ha_post_state()` now says whether it delivered,
and only a delivered reason is remembered.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_shared, solver_writer

ENTITY = "sensor.nimbus_flex_telemetry"
NOW = datetime(2026, 10, 10, 8, 37, tzinfo=UTC)
CALL = {
    "batteries": [],
    "import_price": 0.3,
    "export_price": 0.09,
    "import_limit_kw": 20.0,
    "export_limit_kw": 15.0,
    "period_hours": 1.0,
}
NO_RECORD = solver_writer.flex_telemetry.RecordBuild(
    reason="NEM region not resolved", reason_code="region_unresolved"
)


def _reset() -> None:
    solver_writer.NATIVE.hass = None
    solver_writer._ENTITY_UPDATE_HANDLERS.clear()
    solver_writer._ENTITY_REAL_IDS.clear()
    solver_shared._MANAGED_SKIP_STREAK.clear()
    solver_shared._MANAGED_SKIP_WARNED.clear()
    module = solver_writer.publish_flex_telemetry_record.__globals__
    module["_LAST_FLEX_TELEMETRY_POST"].clear()
    module["_LAST_FLEX_TELEMETRY_NO_RECORD"].clear()


@pytest.fixture
def native():
    _reset()
    hass = MagicMock()
    # run scheduled jobs immediately, as the event loop would
    hass.add_job.side_effect = lambda job: job()
    solver_writer.NATIVE.hass = hass
    try:
        yield hass
    finally:
        _reset()


def _publish():
    solver_writer.publish_flex_telemetry_record(
        {}, SimpleNamespace(grid_signals=None), NOW, **CALL
    )


def test_the_entity_is_managed_so_its_early_posts_are_dropped():
    assert ENTITY in solver_shared._NATIVE_MANAGED_ENTITY_IDS


def test_ha_post_state_says_whether_it_delivered(native):
    assert solver_writer.ha_post_state(ENTITY, "x", {}) is False
    solver_writer.register_entity_handler(ENTITY, lambda s, a: None)
    assert solver_writer.ha_post_state(ENTITY, "x", {}) is True
    # an entity_id that never gets a handler still falls back to a raw write
    assert solver_writer.ha_post_state("sensor.nimbus_unmanaged_x", "x", {}) is True


def test_a_reason_dropped_at_startup_is_sent_once_the_entity_registers(native):
    received: list[tuple] = []
    with patch.object(
        solver_writer, "build_flex_telemetry_record", lambda *_a, **_k: NO_RECORD
    ):
        _publish()  # first solve after restart: entity not registered yet
        assert received == []
        solver_writer.register_entity_handler(
            ENTITY, lambda s, a: received.append((s, a))
        )
        _publish()  # before #1634's fix this was skipped as "already posted"
        _publish()  # and an unchanged, delivered reason is still sent once
    assert len(received) == 1
    state, attrs = received[0]
    assert state == "no_record"
    assert attrs["reason_code"] == "region_unresolved"
