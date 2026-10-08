"""nimbus #1634: the telemetry record's `net_import_kw` comes from the grid
meter when the daily meter reconciliation (#1465) last AGREED with that
same meter, which also settles its sign. Otherwise it stays the
house - solar - battery identity.

Mark Purcell's trial (8 Oct 2026) found the identity 0.077 kW off his
meter, with the opposite sign near zero, and it cannot see an EV charger
outside the house-load sensor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from test_495_flex_telemetry_record import (  # noqa: F401 -- autouse fixture
    _CFG,
    _fresh_publisher_memory,
    _history,
    _real_synthetic_plan,
    _validate,
)

from custom_components.nimbus_load import solver_writer

NOW = datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC)
METER = "sensor.grid_meter"


def _build(cfg, *, reconciliation=None, meter_kw=5.0, quality_raises=False):
    plan, batteries = _real_synthetic_plan(compute_signals=False)
    reads: list[str] = []

    def _fetch(entity_id, start, end):
        values = {
            "sensor.solar": 4.0,
            "sensor.battery": 1.0,  # discharge under _CFG's sign
            "sensor.house": 3.0,
            METER: meter_kw,
        }
        return _history(values[entity_id], start)

    def _ha_get(entity_id):
        reads.append(entity_id)
        if quality_raises:
            raise OSError("quality report unreadable")
        attrs = (
            {} if reconciliation is None else {"meter_reconciliation": reconciliation}
        )
        return {"state": "ok", "attributes": attrs}

    with (
        patch.object(solver_writer, "fetch_entity_history_range", _fetch),
        patch.object(solver_writer, "_kw_scale_factor", lambda _e: 1.0),
        patch.object(solver_writer, "ha_get", _ha_get),
        patch.object(solver_writer, "resolve_real_entity_id", lambda e: e),
    ):
        build = solver_writer.build_flex_telemetry_record(
            dict(cfg),
            plan,
            NOW,
            batteries=batteries,
            import_price=0.30,
            export_price=0.09,
            import_limit_kw=20.0,
            export_limit_kw=15.0,
            period_hours=1.0,
        )
    return build, reads


def _agrees(sign: str, sensor: str = METER) -> dict:
    return {"status": "agrees", "meter_sensor": sensor, "meter_sign": sign}


IDENTITY = -2.0  # 3 house - 4 solar - 1 discharge


def test_an_agreeing_positive_is_import_meter_is_used_as_measured():
    build, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_import"),
    )
    assert build.record is not None, build.reason
    assert build.record["net_import_kw"] == 5.0
    assert build.net_import_source == "grid_meter"
    _validate(build.record)


def test_a_positive_is_export_meter_is_negated():
    build, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_export"),
    )
    assert build.record["net_import_kw"] == -5.0
    assert build.net_import_source == "grid_meter"


def test_the_forecaster_grid_sensor_is_the_fallback_binding():
    build, _ = _build(
        dict(_CFG, grid_sensor=METER), reconciliation=_agrees("positive_is_import")
    )
    assert build.record["net_import_kw"] == 5.0


def test_a_disagreeing_reconciliation_keeps_the_identity():
    rec = dict(_agrees("positive_is_import"), status="disagrees")
    build, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER), reconciliation=rec
    )
    assert build.record["net_import_kw"] == IDENTITY
    assert build.net_import_source == "energy_balance"


def test_a_reconciliation_of_a_different_meter_keeps_the_identity():
    build, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_import", sensor="sensor.other_meter"),
    )
    assert build.record["net_import_kw"] == IDENTITY
    assert build.net_import_source == "energy_balance"


def test_no_reconciliation_yet_keeps_the_identity():
    build, _ = _build(dict(_CFG, switchboard_grid_meter_sensor=METER))
    assert build.record["net_import_kw"] == IDENTITY
    assert build.net_import_source == "energy_balance"


def test_an_unreadable_quality_report_keeps_the_identity():
    build, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER), quality_raises=True
    )
    assert build.record["net_import_kw"] == IDENTITY
    assert build.net_import_source == "energy_balance"


def test_no_meter_configured_reads_nothing_extra():
    build, reads = _build(dict(_CFG))
    assert build.record["net_import_kw"] == IDENTITY
    assert build.net_import_source == "energy_balance"
    assert reads == []


def test_the_source_is_published_beside_the_record_not_in_it():
    build, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_import"),
    )
    assert "net_import_source" not in build.record
    posted = {}

    def _post(entity_id, state, attributes):
        posted.update(attributes)

    with (
        patch.object(
            solver_writer, "build_flex_telemetry_record", lambda *a, **k: build
        ),
        patch.object(solver_writer, "ha_post_state", _post),
    ):
        solver_writer.publish_flex_telemetry_record(
            dict(_CFG),
            None,
            NOW,
            batteries=[],
            import_price=0.30,
            export_price=0.09,
            import_limit_kw=20.0,
            export_limit_kw=15.0,
            period_hours=1.0,
        )
    assert posted["net_import_source"] == "grid_meter"
    assert posted["record"] is build.record
