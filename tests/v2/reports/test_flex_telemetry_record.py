"""The Nimbus-emitted flex telemetry record, schema 2.0 (#489, #495) (#1837)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.nimbus_load import flex_telemetry, solver_writer

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "telemetry.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(record: dict) -> None:
    """The real validator, against the real vendored file."""
    jsonschema.validate(instance=record, schema=_SCHEMA)


# A record the schema accepts, used as the base for the field-level tests
# so each one changes exactly one thing.
def _build(**overrides):
    kwargs = {
        "interval_start": datetime(2026, 9, 27, 4, 35, tzinfo=UTC),
        "region": "QLD1",
        "postcode_prefix": "456",
        "net_import_kw": 0.42,
        "solar_kw": 4.8,
        "house_load_kw": 1.2,
        "deferrable_load_kw": 0.0,
        "naive_baseline_kw": -3.6,
        "price_signal_seen": 0.25,
        "price_export_seen": 0.09,
        "envelope_import_limit_kw": 42.0,
        "envelope_export_limit_kw": 40.0,
        "flex_available_up_kw": 2.1,
        "flex_available_down_kw": 4.3,
        "shadow_energy_price": 0.1644,
        "shadow_solar_forecast_price": 0.0,
        "shadow_envelope_import_price": 0.5393,
        "shadow_envelope_export_price": -0.0087,
        "assets": [],
    }
    kwargs.update(overrides)
    return flex_telemetry.build_record(**kwargs)


class TestTheBaseRecordValidates:
    def test_the_schema_file_is_the_one_this_repo_vendored(self):
        # Guards against the test silently validating against nothing:
        # a missing/emptied file would make jsonschema.validate a no-op.
        assert _SCHEMA["version"] == "2.0"
        assert _SCHEMA["additionalProperties"] is False
        assert (
            flex_telemetry.NAIVE_BASELINE_METHOD
            in (_SCHEMA["properties"]["naive_baseline_method"]["enum"])
        )


class TestAFieldWithNoNimbusSourceIsNull:
    def test_a_null_solar_shadow_still_validates(self):
        # test_a_null_solar_shadow_still_validates
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        build = _build(shadow_solar_forecast_price=None)
        assert build.record is not None
        assert build.record["shadow_solar_forecast_price"] is None
        _validate(build.record)

        # test_build_record_has_no_load_forecast_price_parameter
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        # The absence is the guard: taking it as an argument would invite
        # a caller to pass shadow_energy_price for it.
        import inspect

        params = inspect.signature(flex_telemetry.build_record).parameters
        assert "shadow_load_forecast_price" not in params


class TestClampingIsReportedRatherThanSilent:
    def test_a_price_below_the_floor_is_clamped_and_named(self):
        # test_a_price_below_the_floor_is_clamped_and_named
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        build = _build(price_export_seen=-9.0)
        assert build.record is not None
        assert build.record["price_export_seen"] == flex_telemetry.PRICE_MIN
        assert "price_export_seen" in build.clamped_fields
        _validate(build.record)

        # test_a_nan_in_a_non_nullable_price_refuses_the_record
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        # json.dumps emits NaN as a bare literal that is not valid JSON at
        # all, so a record carrying one would be rejected downstream rather
        # than here. Refused with a reason instead.
        build = _build(price_signal_seen=float("nan"))
        assert build.record is None
        assert "finite" in (build.reason or "")


class TestTheSchemasOwnRangesAreEnforced:
    def test_a_negative_solar_is_clamped_to_zero(self):
        # test_a_negative_solar_is_clamped_to_zero
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        # A real solar CT can read slightly negative at night; the schema
        # says `minimum: 0`.
        build = _build(solar_kw=-0.004)
        assert build.record is not None
        assert build.record["solar_kw"] == 0.0
        _validate(build.record)

        # test_a_bad_postcode_prefix_refuses
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        for bad in (None, "", "12", "1234", "abc", "12a"):
            build = _build(postcode_prefix=bad)
            assert build.record is None, bad
            assert "postcode_prefix" in (build.reason or "")


class TestAssets:
    def _asset(self, **kw):
        clamped: list[str] = []
        kwargs = {
            "name": "home",
            "capacity_kwh": 122.2,
            "soc_kwh": 61.1,
            "charge_kw": 4.0,
            "discharge_kw": 0.0,
            "max_discharge_kw": 40.0,
            "available_up_kw": 36.0,
            "available_down_kw": 40.0,
            "shadow_power_balance_price": 0.1644,
            "clamped": clamped,
        }
        kwargs.update(kw)
        return flex_telemetry.build_asset(**kwargs), clamped

    def test_a_participant_name_is_slugified(self):
        # test_a_participant_name_is_slugified
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        asset, _ = self._asset(name="Tesla Model 3P")
        assert asset["asset_id"] == "tesla_model_3p"

        # test_an_asset_price_clamp_reaches_the_records_own_clamped_list
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        asset, clamped = self._asset(shadow_power_balance_price=99.0)
        build = _build(assets=[asset], clamped_in=clamped)
        assert build.record is not None
        assert any("shadow_power_balance_price" in f for f in build.clamped_fields)
        _validate(build.record)

        # test_a_participant_with_a_departure_or_trips_is_an_ev
        # #2140: the config sensor names the Battery Participants that are
        # vehicles by their own settings; their asset is `ev`, every other
        # battery `stationary_battery`, and the record still validates.
        plan, batteries = _real_synthetic_plan()

        def kinds(cfg):
            build = solver_writer.build_flex_telemetry_record(
                cfg,
                plan,
                datetime(2026, 9, 27, 14, 37, tzinfo=UTC),
                batteries=batteries,
                import_price=0.3,
                export_price=0.09,
                import_limit_kw=20.0,
                export_limit_kw=15.0,
                period_hours=1.0,
                house_load_kw=3.0,
            )
            _validate(build.record)
            return [a["kind"] for a in build.record["assets"]]

        assert kinds(dict(_CFG)) == ["stationary_battery"]
        assert kinds(dict(_CFG, flex_vehicle_participants=["home"])) == ["ev"]
        assert kinds(dict(_CFG, flex_vehicle_participants=["car"])) == [
            "stationary_battery"
        ]


def _period_grid(n: int):
    return solver_writer.elements.PeriodGrid(hours=np.full(n, 1.0), start=None)


def _real_synthetic_plan(compute_signals: bool = True):
    """A genuine `build_plan(...)` solve -- real HiGHS, real duals and
    reduced costs, and real ranging when `compute_signals`."""
    n = 4
    battery = solver_writer.elements.BatteryConfig(
        name="home",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=2.0,
        max_soc_kwh=18.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.05,
    )
    grid = solver_writer.elements.GridConfig(
        import_price=np.array([0.10, 0.10, 0.60, 0.60]),
        export_price=np.array([0.03, 0.03, 0.30, 0.30]),
        import_limit_kw=20.0,
        export_limit_kw=15.0,
    )
    return (
        solver_writer.network.build_plan(
            periods=_period_grid(n),
            grid=grid,
            batteries=[battery],
            solar=solver_writer.elements.SolarConfig(
                forecast_kw=np.array([0.0, 2.0, 6.0, 0.0])
            ),
            loads=[
                solver_writer.elements.LoadConfig(
                    name="house", forecast_kw=np.full(n, 3.0)
                )
            ],
            compute_signals=compute_signals,
        ),
        [battery],
    )


@pytest.fixture(autouse=True)
def _fresh_publisher_memory():
    """The publisher remembers the last interval it posted (#1634 step 3)
    and the last no-record reason. Module state, so cleared for every test
    through the function that actually runs, whichever import path loaded
    it."""
    module = solver_writer.publish_flex_telemetry_record.__globals__
    module["_LAST_FLEX_TELEMETRY_POST"].clear()
    module["_LAST_FLEX_TELEMETRY_NO_RECORD"].clear()
    yield
    module["_LAST_FLEX_TELEMETRY_POST"].clear()
    module["_LAST_FLEX_TELEMETRY_NO_RECORD"].clear()


_CFG = {
    "solver_solar_power_sensor": "sensor.solar",
    "solver_battery_power_sensor": "sensor.battery",
    "solver_whole_house_cross_check_sensor": "sensor.house",
    "solver_battery_power_positive_is_charge": False,
    "region": "QLD1",
    "postcode_prefix": "456",
}


def _history(value: float, start: datetime):
    return [(start + timedelta(seconds=s), value) for s in (10, 100, 200, 290)]


class TestARealSyntheticSolveValidates:
    """#495 acceptance criterion 1."""

    def _record(self, now: datetime, cfg=None, *, house=3.0):
        plan, batteries = _real_synthetic_plan()
        assert plan.status == "optimal"
        assert plan.grid_signals is not None

        return solver_writer.build_flex_telemetry_record(
            dict(cfg or _CFG),
            plan,
            now,
            batteries=batteries,
            import_price=0.30,
            export_price=0.09,
            import_limit_kw=20.0,
            export_limit_kw=15.0,
            period_hours=1.0,
            house_load_kw=house,
        ), plan


class TestNoRecordRatherThanAnInvalidOne:
    def test_no_house_load_means_no_record_and_says_so(self):
        plan, batteries = _real_synthetic_plan()
        build = solver_writer.build_flex_telemetry_record(
            dict(_CFG),
            plan,
            datetime(2026, 9, 27, 14, 37, tzinfo=UTC),
            batteries=batteries,
            import_price=0.3,
            export_price=0.09,
            import_limit_kw=20.0,
            export_limit_kw=15.0,
            period_hours=1.0,
            house_load_kw=None,
        )
        assert build.record is None
        assert build.reason_code == "no_measurement"


class TestThePublishedSensor:
    def _publish(self, cfg=None):
        plan, batteries = _real_synthetic_plan()
        posted: list[tuple] = []

        with patch.object(
            solver_writer,
            "ha_post_state",
            lambda e, s, a: posted.append((e, s, a)),
        ):
            solver_writer.publish_flex_telemetry_record(
                dict(cfg or _CFG),
                plan,
                datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC),
                batteries=batteries,
                import_price=0.30,
                export_price=0.09,
                import_limit_kw=20.0,
                export_limit_kw=15.0,
                period_hours=1.0,
                house_load_kw=1.0,
            )
        return posted

    def test_it_posts_the_documented_entity_id(self):
        # test_it_posts_the_documented_entity_id
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        posted = self._publish()
        assert len(posted) == 1
        assert posted[0][0] == "sensor.nimbus_flex_telemetry"
        assert posted[0][0] == solver_writer.FLEX_TELEMETRY_ENTITY_ID

        # test_a_build_failure_never_takes_the_solve_down
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_POST"
        ].clear()
        solver_writer.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        posted: list[tuple] = []
        with (
            patch.object(
                solver_writer,
                "build_flex_telemetry_record",
                lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
            ),
            patch.object(
                solver_writer, "ha_post_state", lambda e, s, a: posted.append((e, s, a))
            ),
        ):
            solver_writer.publish_flex_telemetry_record(
                dict(_CFG),
                SimpleNamespace(grid_signals=None),
                datetime(2026, 9, 27, 14, 37, tzinfo=UTC),
                batteries=[],
                import_price=0.3,
                export_price=0.09,
                import_limit_kw=20.0,
                export_limit_kw=15.0,
                period_hours=1.0,
            )
        assert posted == []


class TestNoWorkWithoutANewRecord:
    """nimbus #1634: ranging no longer gates the record, so the work is
    gated instead -- no history read when no record can exist, and one
    record per completed interval however often the solver runs."""

    def test_a_second_solve_in_the_same_interval_builds_nothing(self):
        plan, batteries = _real_synthetic_plan(compute_signals=False)
        builds: list[int] = []
        record = {"interval_start_utc": "2026-09-27T04:30:00Z"}
        built = solver_writer.flex_telemetry.RecordBuild(record=record)

        def _build(*_a, **_k):
            builds.append(1)
            return built

        posted: list[tuple] = []
        call = {
            "batteries": batteries,
            "import_price": 0.3,
            "export_price": 0.09,
            "import_limit_kw": 20.0,
            "export_limit_kw": 15.0,
            "period_hours": 1.0,
        }
        with (
            patch.object(solver_writer, "build_flex_telemetry_record", _build),
            patch.object(
                solver_writer, "ha_post_state", lambda e, s, a: posted.append((e, s, a))
            ),
        ):
            # 04:37:12, 04:38:40 and 04:39:55 UTC all have 04:30-04:35Z as
            # their last complete interval: one record, built once.
            for minute, second in ((37, 12), (38, 40), (39, 55)):
                solver_writer.publish_flex_telemetry_record(
                    dict(_CFG),
                    plan,
                    datetime(2026, 9, 27, 4, minute, second, tzinfo=UTC),
                    **call,
                )
        assert len(builds) == 1
        assert len(posted) == 1
