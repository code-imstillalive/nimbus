"""nimbus #1634 (Mark Purcell, 9 Oct 2026): the telemetry record's
`net_import_kw` comes from ONE live read of the grid meter, signed by the
daily meter reconciliation's last AGREEING verdict on that same meter (#1465),
with the plan's period-0 exchange as the fallback. No history is read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from test_495_flex_telemetry_record import (  # noqa: F401 -- autouse fixture
    _CFG,
    _fresh_publisher_memory,
    _real_synthetic_plan,
    _validate,
)

from custom_components.nimbus_load import solver_writer

NOW = datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC)
METER = "sensor.grid_meter"


def _agrees(sign: str, sensor: str = METER) -> dict:
    return {"status": "agrees", "meter_sensor": sensor, "meter_sign": sign}


def _build(cfg, *, reconciliation=None, meter_state="5000", meter_unit="W"):
    plan, batteries = _real_synthetic_plan(compute_signals=False)
    reads: list[str] = []
    history = MagicMock(return_value=[])

    def _ha_get(entity_id):
        reads.append(entity_id)
        if entity_id == solver_writer.QUALITY_ENTITY_ID:
            attrs = (
                {}
                if reconciliation is None
                else {"meter_reconciliation": reconciliation}
            )
            return {"state": "ok", "attributes": attrs}
        return {"state": meter_state, "attributes": {"unit_of_measurement": meter_unit}}

    with (
        patch.object(solver_writer, "ha_get", _ha_get),
        patch.object(solver_writer, "resolve_real_entity_id", lambda e: e),
        patch.object(solver_writer, "fetch_entity_history_range", history),
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
            house_load_kw=3.0,
        )
    planned = float(plan.grid_import_kw[0]) - float(plan.grid_export_kw[0])
    return build, reads, history, planned


def test_an_agreeing_positive_is_import_meter_is_read_live_in_kw():
    build, reads, history, _ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_import"),
    )
    assert build.record is not None, build.reason
    assert build.record["net_import_kw"] == 5.0  # 5000 W
    assert build.net_import_source == "grid_meter"
    assert METER in reads
    assert history.call_count == 0
    _validate(build.record)


def test_a_positive_is_export_meter_is_negated():
    build, *_ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_export"),
        meter_state="2.5",
        meter_unit="kW",
    )
    assert build.record["net_import_kw"] == -2.5


def test_the_forecaster_grid_sensor_is_the_fallback_binding():
    build, *_ = _build(
        dict(_CFG, grid_sensor=METER), reconciliation=_agrees("positive_is_import")
    )
    assert build.net_import_source == "grid_meter"


def test_without_an_agreeing_reconciliation_the_plan_figure_stays():
    for rec in (
        None,
        dict(_agrees("positive_is_import"), status="disagrees"),
        _agrees("positive_is_import", sensor="sensor.other_meter"),
    ):
        build, reads, _h, planned = _build(
            dict(_CFG, switchboard_grid_meter_sensor=METER), reconciliation=rec
        )
        assert build.record["net_import_kw"] == round(planned, 3), rec
        assert build.net_import_source == "plan", rec
        assert METER not in reads, rec  # the meter is not read without a sign


def test_an_unreadable_meter_falls_back_to_the_plan():
    build, _r, _h, planned = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_import"),
        meter_state="unavailable",
    )
    assert build.record["net_import_kw"] == round(planned, 3)
    assert build.net_import_source == "plan"


def test_no_meter_configured_reads_nothing_extra():
    build, reads, _h, _p = _build(dict(_CFG))
    assert build.net_import_source == "plan"
    assert reads == []


def test_the_source_is_published_beside_the_record_not_in_it():
    build, *_ = _build(
        dict(_CFG, switchboard_grid_meter_sensor=METER),
        reconciliation=_agrees("positive_is_import"),
    )
    assert "net_import_source" not in build.record
    posted: dict = {}
    with (
        patch.object(
            solver_writer, "build_flex_telemetry_record", lambda *a, **k: build
        ),
        patch.object(solver_writer, "ha_post_state", lambda e, s, a: posted.update(a)),
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
