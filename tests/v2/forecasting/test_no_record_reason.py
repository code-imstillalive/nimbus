"""The flex telemetry record says why it has no record, and a missing one is
retried, never left `unknown` silently (#1634, #1635) (#1837).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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


def test_the_entity_is_managed_so_its_early_posts_are_dropped(native):
    # test_the_entity_is_managed_so_its_early_posts_are_dropped
    assert ENTITY in solver_shared._NATIVE_MANAGED_ENTITY_IDS

    # test_ha_post_state_says_whether_it_delivered
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

    # test_an_unchanged_reason_is_sent_again_each_interval
    # nimbus #2133: sent once per reason only, the sensor went stale 5 minutes
    # later and read `unavailable` for as long as the reason lasted -- four
    # hours on the reference household -- hiding the reason.
    with patch.object(
        solver_writer, "build_flex_telemetry_record", lambda *_a, **_k: NO_RECORD
    ):
        for minutes in (0, 3, 5, 10):
            solver_writer.publish_flex_telemetry_record(
                {},
                SimpleNamespace(grid_signals=None),
                NOW.replace(minute=40) + timedelta(minutes=minutes),
                **CALL,
            )
    # 08:40 and 08:43 share an interval; 08:45 and 08:50 are new ones.
    assert len(received) == 1 + 3
