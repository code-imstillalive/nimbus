"""nimbus issue #495 (Signals 6/7 of #489): the Nimbus-emitted
`nem-flex-telemetry` schema-v2.0 record.

Every assertion here is against the **vendored schema file**
(`schema/telemetry.schema.json`, pinned to upstream `024f45c6` by sha256,
provenance in `schema/PROVENANCE.md`) using the real `jsonschema`
validator -- never against a reconstruction of it, and never against a
hand-written checker. That is this issue's own long-standing condition:
it sat blocked for days rather than validate against a summary of the
schema, on the grounds that such a test *"would pass by construction and
prove nothing"*. A hand-rolled validator is the same failure one layer
down; it would assert the record matches whichever subset of the schema
the validator's author implemented.

The three acceptance criteria on #495, and where each lives:

1. *"A record built from a synthetic solve validates against
   telemetry.schema.json v2.0."* -> `TestARealSyntheticSolveValidates`,
   which runs `build_plan(..., compute_signals=True)` for real (HiGHS,
   real ranging) rather than faking a `Plan`.
2. *"Two consecutive records are exactly 300 s apart on :00/:05/...
   boundaries regardless of when the solve tick ran."* ->
   `TestBoundaryAlignment`.
3. *"With the counterfactual baseline, a charging interval reports a
   non-negative counterfactual saving at a cheap price."* -> **cannot be
   met as written, and `TestTheBaselineIsHonestAboutCharging` pins the
   honest form instead.** The baseline method was settled as
   `"subtraction"` by @purcell-lab on 2026-09-13 (the schema's enum is
   `["subtraction", "haeo_counterfactual"]`; `nimbus_counterfactual` is
   not a member), and NO battery-excluding baseline can show a saving on
   a charging interval -- charging at that instant genuinely IS extra
   import. The saving lands on discharge. So the interval-level assertion
   is that the sign is honest, and the day-level assertion
   (`TestTheBaselineNetsOutOverADay`) is that the household beats the
   baseline once both legs are counted. Stated on the PR rather than
   quietly passing something adjacent.
"""

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.nimbus_load import flex_telemetry, solver_writer

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schema" / "telemetry.schema.json"
)
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
    def test_it_validates(self):
        build = _build()
        assert build.record is not None, build.reason
        _validate(build.record)

    def test_the_schema_file_is_the_one_this_repo_vendored(self):
        # Guards against the test silently validating against nothing:
        # a missing/emptied file would make jsonschema.validate a no-op.
        assert _SCHEMA["version"] == "2.0"
        assert _SCHEMA["additionalProperties"] is False
        assert (
            flex_telemetry.NAIVE_BASELINE_METHOD
            in (_SCHEMA["properties"]["naive_baseline_method"]["enum"])
        )

    def test_the_record_carries_exactly_the_required_keys_and_no_extras(self):
        build = _build()
        assert build.record is not None
        # `additionalProperties: false` means one extra or misnamed key is
        # a hard failure, so the closed-dict contract is asserted directly
        # rather than trusted to the validator alone.
        assert set(build.record) == set(_SCHEMA["required"])


class TestAFieldWithNoNimbusSourceIsNull:
    def test_shadow_load_forecast_price_is_always_null(self):
        # Nimbus has no load-forecast LIMIT -- its load forecast enters
        # power_balance_t as a constant, so the derivative IS
        # shadow_energy_price, already in the record. Publishing the same
        # number twice would claim two independent measurements.
        build = _build()
        assert build.record is not None
        assert build.record["shadow_load_forecast_price"] is None
        _validate(build.record)

    def test_a_null_solar_shadow_still_validates(self):
        build = _build(shadow_solar_forecast_price=None)
        assert build.record is not None
        assert build.record["shadow_solar_forecast_price"] is None
        _validate(build.record)

    def test_build_record_has_no_load_forecast_price_parameter(self):
        # The absence is the guard: taking it as an argument would invite
        # a caller to pass shadow_energy_price for it.
        import inspect

        params = inspect.signature(flex_telemetry.build_record).parameters
        assert "shadow_load_forecast_price" not in params


class TestClampingIsReportedRatherThanSilent:
    def test_a_price_above_the_cap_is_clamped_and_named(self):
        build = _build(price_signal_seen=50.0)
        assert build.record is not None
        assert build.record["price_signal_seen"] == flex_telemetry.PRICE_MAX
        assert "price_signal_seen" in build.clamped_fields
        _validate(build.record)

    def test_a_price_below_the_floor_is_clamped_and_named(self):
        build = _build(price_export_seen=-9.0)
        assert build.record is not None
        assert build.record["price_export_seen"] == flex_telemetry.PRICE_MIN
        assert "price_export_seen" in build.clamped_fields
        _validate(build.record)

    def test_an_in_range_record_reports_no_clamping(self):
        assert _build().clamped_fields == ()

    def test_a_nan_in_a_non_nullable_price_refuses_the_record(self):
        # json.dumps emits NaN as a bare literal that is not valid JSON at
        # all, so a record carrying one would be rejected downstream rather
        # than here. Refused with a reason instead.
        build = _build(price_signal_seen=float("nan"))
        assert build.record is None
        assert "finite" in (build.reason or "")

    def test_a_nan_in_a_nullable_shadow_becomes_null(self):
        build = _build(shadow_energy_price=float("nan"))
        assert build.record is not None
        assert build.record["shadow_energy_price"] is None
        assert "shadow_energy_price" in build.clamped_fields
        _validate(build.record)


class TestTheSchemasOwnRangesAreEnforced:
    def test_a_negative_solar_is_clamped_to_zero(self):
        # A real solar CT can read slightly negative at night; the schema
        # says `minimum: 0`.
        build = _build(solar_kw=-0.004)
        assert build.record is not None
        assert build.record["solar_kw"] == 0.0
        _validate(build.record)

    def test_a_bad_region_refuses_rather_than_guessing(self):
        build = _build(region="WA1")
        assert build.record is None
        assert "region" in (build.reason or "")

    def test_a_missing_region_refuses(self):
        assert _build(region=None).record is None

    def test_a_bad_postcode_prefix_refuses(self):
        for bad in (None, "", "12", "1234", "abc", "12a"):
            build = _build(postcode_prefix=bad)
            assert build.record is None, bad
            assert "postcode_prefix" in (build.reason or "")


class TestBoundaryAlignment:
    """#495 acceptance criterion 2."""

    def test_any_tick_offset_inside_an_interval_yields_the_same_record_window(self):
        base = datetime(2026, 9, 27, 14, 35, tzinfo=UTC)
        starts = {
            flex_telemetry.last_complete_interval_start(base + offset)
            for offset in (
                timedelta(0),
                timedelta(seconds=1),
                timedelta(seconds=37, microseconds=500000),
                timedelta(seconds=299, microseconds=999999),
            )
        }
        assert starts == {datetime(2026, 9, 27, 14, 30, tzinfo=UTC)}

    def test_two_consecutive_records_are_exactly_300s_apart(self):
        # Deliberately NOT two ticks 300 s apart: two ticks at arbitrary,
        # unequal offsets inside consecutive intervals, which is the real
        # case the criterion is about.
        a = _build(
            interval_start=flex_telemetry.last_complete_interval_start(
                datetime(2026, 9, 27, 14, 35, 2, tzinfo=UTC)
            )
        ).record
        b = _build(
            interval_start=flex_telemetry.last_complete_interval_start(
                datetime(2026, 9, 27, 14, 44, 51, tzinfo=UTC)
            )
        ).record
        assert a is not None and b is not None
        ta = datetime.strptime(a["interval_start_utc"], "%Y-%m-%dT%H:%M:%S%z")
        tb = datetime.strptime(b["interval_start_utc"], "%Y-%m-%dT%H:%M:%S%z")
        assert (tb - ta) == timedelta(seconds=flex_telemetry.INTERVAL_SECONDS)
        assert ta.minute % 5 == 0 and tb.minute % 5 == 0
        assert ta.second == 0 and tb.second == 0

    def test_the_timestamp_uses_the_schemas_own_z_suffix(self):
        record = _build().record
        assert record is not None
        # `datetime.isoformat()` would produce "+00:00", which does not
        # match the schema's own examples.
        assert record["interval_start_utc"].endswith("Z")
        assert "+" not in record["interval_start_utc"]

    def test_a_non_utc_tick_is_converted_before_flooring(self):
        # UTC+10, no DST -- the reference household's own zone. 14:32:10
        # AEST is 04:32:10 UTC, so the last complete interval is 04:25 UTC.
        from zoneinfo import ZoneInfo

        aest = datetime(2026, 9, 27, 14, 32, 10, tzinfo=ZoneInfo("Australia/Brisbane"))
        assert flex_telemetry.last_complete_interval_start(aest) == datetime(
            2026, 9, 27, 4, 25, tzinfo=UTC
        )


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

    def test_an_asset_validates_inside_a_record(self):
        asset, _ = self._asset()
        build = _build(assets=[asset])
        assert build.record is not None
        _validate(build.record)

    def test_the_home_pack_gets_the_schemas_own_slug(self):
        asset, _ = self._asset()
        assert asset["asset_id"] == "home_battery"

    def test_a_participant_name_is_slugified(self):
        asset, _ = self._asset(name="Tesla Model 3P")
        assert asset["asset_id"] == "tesla_model_3p"

    def test_setpoint_is_positive_when_charging(self):
        # The schema's convention, and the OPPOSITE of this household's own
        # sensor.logger_battery_power -- so it is asserted, not assumed.
        asset, _ = self._asset(charge_kw=4.0, discharge_kw=0.0)
        assert asset["setpoint_kw"] == 4.0

    def test_setpoint_is_negative_when_discharging(self):
        asset, _ = self._asset(charge_kw=0.0, discharge_kw=11.5)
        assert asset["setpoint_kw"] == -11.5

    def test_bidirectional_is_derived_from_the_discharge_ceiling(self):
        assert self._asset(max_discharge_kw=40.0)[0]["bidirectional_capable"] is True
        assert self._asset(max_discharge_kw=0.0)[0]["bidirectional_capable"] is False

    def test_soc_pct_is_a_percentage_of_capacity(self):
        asset, _ = self._asset(capacity_kwh=122.2, soc_kwh=61.1)
        assert abs(asset["soc_pct"] - 50.0) < 1e-6

    def test_a_zero_capacity_asset_does_not_divide_by_zero(self):
        asset, _ = self._asset(capacity_kwh=0.0, soc_kwh=0.0)
        assert asset["soc_pct"] == 0.0
        _validate(_build(assets=[asset]).record)

    def test_every_asset_is_stationary_until_467_lands(self):
        # BatteryConfig has no EV flag; guessing "ev" from a name is the
        # inference @purcell-lab rejected on #768.
        asset, _ = self._asset(name="ev_m3p")
        assert asset["kind"] == "stationary_battery"

    def test_an_asset_price_clamp_reaches_the_records_own_clamped_list(self):
        asset, clamped = self._asset(shadow_power_balance_price=99.0)
        build = _build(assets=[asset], clamped_in=clamped)
        assert build.record is not None
        assert any("shadow_power_balance_price" in f for f in build.clamped_fields)
        _validate(build.record)


class TestTheBaselineIsHonestAboutCharging:
    """#495 acceptance criterion 3, in the only form it can hold."""

    def test_the_baseline_is_load_minus_solar_not_load_alone(self):
        # Upstream's own v0.3 baseline is house load ALONE, which counts
        # every kWh of self-consumed solar as a loss -- the
        # `total_savings_aud: -5083.37` artefact #489 measured. Crediting
        # solar is what fixes it, and the discriminating case is a
        # solar-surplus interval: load-alone can never be negative.
        load_kw, solar_kw = 1.2, 4.8
        baseline = load_kw - solar_kw
        build = _build(
            house_load_kw=load_kw, solar_kw=solar_kw, naive_baseline_kw=baseline
        )
        assert build.record is not None
        assert build.record["naive_baseline_kw"] < 0.0
        assert build.record["naive_baseline_kw"] != build.record["house_load_kw"]
        _validate(build.record)

    def test_a_charging_interval_reports_more_import_than_the_baseline(self):
        # The criterion as literally written asks for a non-negative saving
        # here. It cannot hold: a battery-excluding baseline necessarily
        # shows charging as extra import. Pinned so a future "fix" that
        # folds the battery into the baseline fails here and has to argue
        # for itself.
        load_kw, solar_kw, charge_kw = 1.2, 0.0, 4.0
        baseline = load_kw - solar_kw
        net_import = load_kw - solar_kw + charge_kw
        assert net_import > baseline
        build = _build(
            house_load_kw=load_kw,
            solar_kw=solar_kw,
            naive_baseline_kw=baseline,
            net_import_kw=net_import,
        )
        assert build.record is not None
        assert build.record["net_import_kw"] > build.record["naive_baseline_kw"]

    def test_the_method_is_the_settled_enum_member(self):
        record = _build().record
        assert record is not None
        assert record["naive_baseline_method"] == "subtraction"


class TestTheBaselineNetsOutOverADay:
    """The day-level form of criterion 3 -- the one that is actually true.

    A charge-cheap / discharge-expensive day, priced interval by interval
    through the record's own two fields. If the baseline is defined
    correctly, the household's own cost is BELOW the no-battery baseline's
    once both legs are counted, even though every charging interval on its
    own looked worse.
    """

    def test_the_household_beats_the_no_battery_baseline_over_a_full_day(self):
        hours = 1.0 / 12.0  # one 5-minute interval
        cheap, dear = 0.05, 0.60
        load_kw = 2.0
        baseline_cost = actual_cost = 0.0
        charging_intervals_that_looked_worse = 0
        for i in range(24 * 12):
            price = cheap if 10 * 12 <= i < 13 * 12 else dear
            charge_kw = 10.0 if 10 * 12 <= i < 13 * 12 else 0.0
            discharge_kw = 10.0 if 17 * 12 <= i < 20 * 12 else 0.0
            baseline = load_kw
            net_import = load_kw + charge_kw - discharge_kw
            build = _build(
                house_load_kw=load_kw,
                solar_kw=0.0,
                naive_baseline_kw=baseline,
                net_import_kw=net_import,
                price_signal_seen=price,
            )
            assert build.record is not None
            baseline_cost += build.record["naive_baseline_kw"] * price * hours
            actual_cost += build.record["net_import_kw"] * price * hours
            if charge_kw and build.record["net_import_kw"] > baseline:
                charging_intervals_that_looked_worse += 1
        assert charging_intervals_that_looked_worse == 3 * 12
        assert actual_cost < baseline_cost


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

        def _no_history(*_a, **_k):
            raise AssertionError("the record must not read history (#1634)")

        with patch.object(solver_writer, "fetch_entity_history_range", _no_history):
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

    def test_the_record_validates_against_the_vendored_schema(self):
        build, _ = self._record(datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC))
        assert build.record is not None, build.reason
        _validate(build.record)

    def test_it_carries_one_asset_per_battery_the_lp_planned(self):
        build, _ = self._record(datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC))
        assert build.record is not None
        assert [a["asset_id"] for a in build.record["assets"]] == ["home_battery"]
        asset = build.record["assets"][0]
        assert asset["capacity_kwh"] == 20.0
        assert 0.0 <= asset["soc_pct"] <= 100.0

    def test_the_site_figures_are_the_solves_own_period_0(self):
        """nimbus #1634 (Mark Purcell, 8 Oct 2026): the record publishes what
        Nimbus already holds, with no history reads. House load is the
        solve's period-0 load (the live reading), solar the plan's period-0
        solar, net import the plan's period-0 exchange."""
        build, plan = self._record(
            datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC), house=3.0
        )
        assert build.record is not None
        solar = float(plan.solar_used_kw[0]) + float(plan.solar_curtailed_kw[0])
        net = float(plan.grid_import_kw[0]) - float(plan.grid_export_kw[0])
        assert build.record["house_load_kw"] == 3.0
        assert build.record["solar_kw"] == round(solar, 3)
        assert build.record["net_import_kw"] == round(net, 3)
        assert build.record["naive_baseline_kw"] == round(3.0 - solar, 3)

    def test_no_power_sensor_needs_to_be_configured(self):
        cfg = {"region": "QLD1", "postcode_prefix": "456"}
        build, _ = self._record(datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC), cfg)
        assert build.record is not None, build.reason
        _validate(build.record)

    def test_the_envelope_shadows_come_from_the_grid_signals(self):
        plan, _ = _real_synthetic_plan()
        gs = plan.grid_signals
        assert gs is not None
        build, _ = self._record(datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC))
        assert build.record is not None
        assert build.record["shadow_envelope_import_price"] == round(
            float(gs.forced_import_cost[0]), 4
        )
        assert build.record["shadow_envelope_export_price"] == round(
            float(gs.forced_export_cost[0]), 4
        )


class TestNoRecordRatherThanAnInvalidOne:
    def test_no_ranging_still_builds_a_valid_record_with_physical_flex(self):
        """nimbus #1634 step 1 (Mark Purcell, 8 Oct 2026): ranging stays off
        by default, and the record no longer needs it. `flex_available_*`
        is physical availability (sum of battery hardware headroom, capped
        by the envelope at the planned exchange); the two envelope shadow
        prices, which only ranging produces, are null."""
        plan, batteries = _real_synthetic_plan(compute_signals=False)
        assert plan.grid_signals is None

        build = solver_writer.build_flex_telemetry_record(
            dict(_CFG),
            plan,
            datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC),
            batteries=batteries,
            import_price=0.30,
            export_price=0.09,
            import_limit_kw=20.0,
            export_limit_kw=15.0,
            period_hours=1.0,
            house_load_kw=3.0,
        )
        assert build.record is not None, build.reason
        _validate(build.record)
        rec = build.record
        assert rec["shadow_envelope_import_price"] is None
        assert rec["shadow_envelope_export_price"] is None
        net = float(plan.grid_import_kw[0]) - float(plan.grid_export_kw[0])
        up = sum(float(b.available_up_kw[0]) for b in plan.battery_signals)
        down = sum(float(b.available_down_kw[0]) for b in plan.battery_signals)
        assert rec["flex_available_up_kw"] == round(max(0.0, min(up, 20.0 - net)), 3)
        assert rec["flex_available_down_kw"] == round(
            max(0.0, min(down, 15.0 + net)), 3
        )
        # A 5 kW battery has kilowatts of headroom; ranging's sub-kW
        # basis-change figure (#1634 finding 1) is not what is reported.
        assert rec["flex_available_up_kw"] + rec["flex_available_down_kw"] > 1.0

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
        posted = self._publish()
        assert len(posted) == 1
        assert posted[0][0] == "sensor.nimbus_flex_telemetry"
        assert posted[0][0] == solver_writer.FLEX_TELEMETRY_ENTITY_ID

    def test_the_state_is_the_interval_start(self):
        _entity_id, state, attrs = self._publish()[0]
        assert state == "2026-09-27T14:30:00Z"
        assert state == attrs["record"]["interval_start_utc"]

    def test_the_record_is_one_nested_attribute_not_spread(self):
        _entity_id, _state, attrs = self._publish()[0]
        # The whole point of nesting: what a provider POSTs is
        # attributes.record, unedited, and it validates as-is.
        _validate(attrs["record"])
        # None of the record's own keys leak into the attribute dict, where
        # they would sit beside friendly_name/nimbus_version and stop
        # validating if anyone tried to POST the attributes wholesale.
        assert not (set(attrs) - {"record"}) & set(attrs["record"])

    def test_no_record_publishes_its_reason_once(self):
        """nimbus #1634 step 2: a consumer can tell WHY there is no record
        without DEBUG logging, and an unchanged reason is not re-posted
        every solve."""
        # The module that actually runs, whichever import path loaded it.
        module = solver_writer.publish_flex_telemetry_record.__globals__
        module["_LAST_FLEX_TELEMETRY_NO_RECORD"].clear()
        module["_LAST_FLEX_TELEMETRY_POST"].clear()
        posted: list[tuple] = []
        call = {
            "batteries": [],
            "import_price": 0.3,
            "export_price": 0.09,
            "import_limit_kw": 20.0,
            "export_limit_kw": 15.0,
            "period_hours": 1.0,
        }
        no_record = solver_writer.flex_telemetry.RecordBuild(
            reason="no measured history for the interval", reason_code="no_history"
        )
        with (
            patch.object(
                solver_writer, "ha_post_state", lambda e, s, a: posted.append((e, s, a))
            ),
            patch.object(
                solver_writer,
                "build_flex_telemetry_record",
                lambda *_a, **_k: no_record,
            ),
        ):
            for _ in range(3):
                solver_writer.publish_flex_telemetry_record(
                    dict(_CFG),
                    SimpleNamespace(grid_signals=None),
                    datetime(2026, 9, 27, 14, 37, tzinfo=UTC),
                    **call,
                )
        assert len(posted) == 1
        entity, state, attrs = posted[0]
        assert entity == solver_writer.FLEX_TELEMETRY_ENTITY_ID
        assert state == "no_record"
        assert attrs["reason_code"] == "no_history"
        assert attrs["reason"] == "no measured history for the interval"
        assert "record" not in attrs
        assert attrs["last_record_interval"] is None
        module["_LAST_FLEX_TELEMETRY_NO_RECORD"].clear()

    def test_a_build_failure_never_takes_the_solve_down(self):
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

    def test_an_unresolved_region_reads_no_history(self):
        plan, batteries = _real_synthetic_plan(compute_signals=False)
        reads: list[str] = []

        def _fetch(entity_id, *_a):
            reads.append(entity_id)
            return []

        with patch.object(solver_writer, "fetch_entity_history_range", _fetch):
            build = solver_writer.build_flex_telemetry_record(
                dict(_CFG, region=None),
                plan,
                datetime(2026, 9, 27, 14, 37, 12, tzinfo=UTC),
                batteries=batteries,
                import_price=0.30,
                export_price=0.09,
                import_limit_kw=20.0,
                export_limit_kw=15.0,
                period_hours=1.0,
            )
        assert build.record is None
        assert build.reason_code == "region_unresolved"
        assert reads == []

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
