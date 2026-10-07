"""Golden-output guardrail for nimbus issue #363's own agreed staged-
extraction plan (Mark Purcell, codebase review, split() finding 5 --
main()'s 1,788 lines split into ha_bridge/solver_inputs/solver_plan/
solver_publish/solver_reports modules): step 1 of the plan he proposed
and this repo's own maintainer explicitly agreed to follow exactly --

    "Guardrails first, before moving a line. [...] Add one golden-output
    test: a fixed set of source-sensor states in, and the exact pushed
    sensor.nimbus_solver_battery_forecast attributes out (plan, cost
    band, breakdown). Any extraction that changes a byte of that output
    fails."

Drives the REAL main() (not a reimplementation) with a full ha_get mock
-- the same minimal fixture shape test_main_load_forecast_startup_race.py
already established, extended just enough to reach a genuine, real
"optimal" LP solve rather than stopping at an earlier branch -- and
asserts on the exact `sensor.nimbus_solver_battery_forecast` state and
attributes `ha_post_state` receives.

Time is frozen (`@pytest.mark.freeze_time`): confirmed empirically before
writing this test that main()'s own `now = datetime.now(UTC)...` call
(solver_writer.py, main()'s own first lines) feeds `build_tiered_grid()`,
whose OWN period count/alignment genuinely depends on real wall-clock
time (observed directly: two runs a few minutes apart produced 360 vs.
365 periods and different total_cost) -- this is NOT a coincidence-prone
flaky test, freezing is a real, load-bearing requirement for
reproducibility, not a cosmetic nicety.

Every persisted state path main() touches is redirected into a per-test
temporary directory (nimbus issue #1330). This is the same kind of
load-bearing requirement as freezing the clock above, and for the same
reason -- an unpinned state path is an INPUT to a golden comparison.
Before this, `PLAN_STATE_PATH` pointed at a fixed
`/tmp/nonexistent_plan_state_golden_test.json` whose name asserted the
invariant it broke: main() WRITES that file, so run two on the same
machine read run one's plan back through the proximal term and
`forecast[1]['shadow_price']` became 0.2999 against the pinned 0.3
(found by Mark Purcell while building the golden-master harness, #1328).
CI never saw it because a CI runner always starts clean -- so the only
place it bit was a developer running the suite twice, which is precisely
when someone is iterating on this code.

Worse, `SOLAR_DELIVERY_RATIO_PATH` was not redirected at all, and
solver_writer both reads and writes it. Its default is
`/opt/nimbus_solver_solar_delivery_ratio.json` -- absent on CI and on a
dev box, so the failure was caught and invisible, but real on either NUC,
where this test would have read the live solar-delivery ratio into the
golden comparison and then overwritten it. A test that mutates production
state, silent in exactly the environments where it is harmless.

Redirecting the whole set, rather than the one path that was noticed,
is what makes a third such path safe by construction.

Deliberately excludes from comparison: every `forecast[i]['time']` ISO
string and the top-level `generated_at` field, both of which are still
literally the frozen instant re-formatted per period offset -- keeping
them out of the asserted dict makes failures point at genuine VALUE
drift, not an incidental formatting/timezone-library version difference
this test was never meant to guard.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_publish
import solver_shared
import solver_writer
from _isolated_state import isolated_state_paths

_LOAD_SENSOR = "sensor.nimbus_sigen_plant_total_load_power_forecast"

_SOLVER_CONFIG_ATTRS = {
    "solver_battery_soc_sensor": "sensor.fake_soc",
    "solver_battery_capacity_kwh": 40.0,
    "solver_max_charge_kw": 5.0,
    "solver_max_discharge_kw": 5.0,
    "solver_grid_max_import_kw": 15.0,
    "solver_grid_max_export_kw": 15.0,
    "solver_import_price_sensor": "sensor.fake_import_price",
    "solver_export_price_sensor": "sensor.fake_export_price",
    "solver_solar_forecast_sensor": "",
    "solver_load_forecast_sensor": _LOAD_SENSOR,
    "solver_load_forecast_entities": [],
}

_HEALTHY_LOAD_STATE = {
    "state": "1.5",
    "attributes": {
        "unit_of_measurement": "kW",
        "forecast": [
            {"time": "2026-09-06T20:30:00+10:00", "value": 1.5},
            {"time": "2026-09-06T20:45:00+10:00", "value": 1.4},
            {"time": "2026-09-06T21:00:00+10:00", "value": 1.3},
        ],
    },
}

# Every non-timestamp key expected on the real (state, attrs) tuple
# main() pushes to sensor.nimbus_solver_battery_forecast for this exact
# fixture -- captured directly from a real run against this same
# fixture, frozen at the timestamp this test itself freezes to.
#
# nimbus issue #451 (Mark Purcell): regenerated 2026-09-07 after
# reshaping build_tiered_grid() (tier1 now boundary-snapped to the real
# current+next NEM trading interval instead of a fixed 24h/360-period
# grid) -- a real, deliberate, understood output change, not drift.
# n_periods 360 -> 202 (matching #451's own ~200-206 estimate) shifts
# every cost/dispatch number that depends on the exact grid shape.
# load_forecast_coverage_hours stays 1.0 (this fixture's own real
# forecast points -- 20:30/20:45/21:00 -- still sit inside tier1's own
# shorter span either way).
_EXPECTED_STATE = -5.0
_EXPECTED_ATTRS = {
    "unit_of_measurement": "kW",
    "device_class": "power",
    "state_class": "measurement",
    "friendly_name": "Nimbus Solver Battery Forecast",
    "signal_role": "battery",
    "source_sensor": "sensor.fake_soc",
    "status": "optimal",
    # nimbus issue #696, Stage 2: regenerated from a fresh real run
    # after solver_writer.py's main() started defaulting to
    # solve_options=CalibratedOptions() (switch.nimbus_solver_
    # calibrated_objective_enabled, default on) -- proximal_weight/
    # smoothness_weight/battery_charge_earliness_budget_kw now resolve
    # on the LP's own SECONDARY channel with a searched-safe blend
    # weight, rather than the old hand-picked magnitude summed directly
    # into the primary objective. A small, real, understood shift (the
    # searched weight isn't byte-identical to the old 0.005 constant),
    # not drift -- see network.py's own build_plan() docstring on
    # `solve_options` for the full mechanism.
    # nimbus issue #732: regenerated from a fresh real run after adding
    # a real, price-aware efficiency-loss cost to every battery's own
    # charge/discharge (capped at MIN_CHARGE_DISCHARGE_COST_SPREAD) --
    # this fixture's own single battery now cycles genuinely less
    # (real, deliberate throughput reduction, not drift): the LP now
    # correctly prices the round-trip loss it was previously only
    # feeling indirectly through terminal/salvage value.
    # nimbus issue #776: regenerated from a fresh real run after fixing
    # CalibratedOptions' own final blended solve leaking a mixed-unit
    # `primary + weight * secondary` value into LPResult.objective/
    # Plan.total_cost -- this fixture's own secondary costs (proximal_
    # weight/smoothness_weight/battery_charge_earliness_budget_kw) are
    # small-scale, so the pre-fix contamination here was tiny (~0.0008
    # out of ~27, versus the ~$1295-of-$1335 real production case #776
    # itself was filed against); total_cost/total_cost_with_fixed_costs
    # now report the real primary-only dollar cost of the returned
    # solution, and terminal_value_credit (a RESIDUAL against
    # total_cost, see solver_writer.py's cost_breakdown() docstring)
    # shifts by the same tiny amount as a direct consequence.
    # nimbus issue #756-golden-CI-flake: total_cost is now rounded to 4dp
    # at the publish site (solver_writer.py), matching every sibling KPI's
    # own precision -- HiGHS's LP solve is not bit-for-bit deterministic
    # run to run (this exact fixture flaked on CI: 26.987417226270225 vs
    # 26.98741722627023, a ~1e-13 difference), which the raw unrounded
    # value was passing straight through into this guardrail.
    "total_cost": 26.9874,
    "total_cost_with_fixed_costs": 34.7874,
    # nimbus issue #781: regenerated after adding the explicit
    # `soc_penalty` line item to cost_breakdown() -- 0.0 here since this
    # fixture's own single battery never dips below its configured floor;
    # total_cost/terminal_value_credit are unchanged.
    # nimbus issue #788: regenerated again after adding the explicit
    # `grid_import_excess_penalty` line item -- 0.0 here since this
    # fixture's own fixed inputs never need the excess-import release
    # valve (#390); total_cost/terminal_value_credit are unchanged.
    # nimbus issue #937 item 4: regenerated after adding the forecast-source
    # decision attributes. Purely ADDITIVE -- a probe of the delta reported 7
    # new keys, ZERO missing and ZERO changed, which is the evidence that the
    # change does not move any number that already existed.
    #
    # The values are themselves the proof the feature ships gated OFF:
    # policy "off", selected "ml" (not persistence), weight 0.0, and a null
    # mean because no day has been scored yet on a fresh fixture. An install
    # that does nothing sees exactly today's behaviour.
    "load_forecast_persistence_weight": 0.0,
    "load_forecast_source_days_persistence_won": 0,
    "load_forecast_source_days_scored": 0,
    "load_forecast_source_mean_value_add_dollars": None,
    "load_forecast_source_policy": "off",
    "load_forecast_source_reason": "policy_off",
    "load_forecast_source_selected": "ml",
    "cost_breakdown": {
        "grid_net": 33.5015,
        "degradation": 0.0,
        "charge_fee": 0.127,
        "discharge_fee": 0.26,
        "soc_penalty": 0.0,
        "grid_import_excess_penalty": 0.0,
        "terminal_value_credit": -6.9011,
    },
    "cost_band": {"lower": 1.741, "upper": 32.7335, "width": 30.9925},
    "cost_band_24h": {"lower": -1.2203, "upper": 8.1922, "width": 9.4125},
    "p2p_match_fraction": 0.0,
    "risk_aversion": 0.25,
    "import_price_risk_aversion": 0.0,
    "export_price_risk_aversion": 0.0,
    "risk_aversion_active": True,
    "salvage_value": 0.15,
    "degradation_cost_per_kwh": 0.0,
    "total_charge_kwh": 12.7,
    "total_discharge_kwh": 26.0,
    "total_throughput_kwh": 38.7,
    "equivalent_full_cycles": 0.484,
    "battery_kw_side": "AC",
    "battery_kw_sign_convention": "positive_discharge_negative_charge",
    "efficiency_convention": "round_trip_symmetric_sqrt",
    "price_blend_algorithm": "primary_preferring_fallback_to_secondary_mean",
    "charge_efficiency": 0.9747,
    "discharge_efficiency": 0.9747,
    "ac_bus_losses_kwh": 0.997,
    "n_periods": 202,
    "n_clamped_periods": 0,
    # nimbus issue #652: this fixture is a single-battery, no-
    # controllable-load scenario -- 1 battery (the home pack), 0
    # sheddable/adequacy/thermal loads (no controllable_load subentries
    # in this fixture's own config).
    #
    # nimbus issue #773 (2026-09-14): the per-kind breakdown was added
    # after n_controllable_loads was found under-reporting on a real
    # install -- it summed only sheddable + adequacy, omitting thermal
    # loads entirely. All four stay 0 here because this fixture
    # genuinely has no controllable loads of any kind, so this entry is
    # unchanged in substance.
    "solve_diagnostics": {
        "n_batteries": 1,
        "n_periods": 202,
        "n_controllable_loads": 0,
        "n_sheddable_loads": 0,
        "n_adequacy_loads": 0,
        "n_thermal_loads": 0,
        # nimbus issue #1386: empty, and empty is the meaningful value --
        # this fixture configures no thermal load at all, so there is no
        # hard temperature guarantee to relax. An EMPTY LIST rather than an
        # absent key is the point: it distinguishes "nothing relaxed this
        # cycle" from "a release that does not report relaxation", which a
        # reader of recorder history months later cannot otherwise tell
        # apart.
        "thermal_guarantee_relaxed": [],
        # nimbus issue #485: None here is correct and meaningful -- this
        # fixture has no live select entity, and the honest answer is
        # "no mode was in force" rather than defaulting to "home" and
        # implying one was.
        "household_mode": None,
        # nimbus issue #1013. This fixture never sets a State of Health,
        # so it takes DEFAULT_SOLVER_SOH_PERCENT and effective capacity
        # equals nameplate exactly -- which is the point worth noticing
        # here: the whole rest of this golden output is byte-identical
        # across that change, so the derating really is a no-op for an
        # install that has not touched the dial. Only these three new
        # keys moved, and this guardrail is what proved it.
        "battery_soh_percent": 100.0,
        "battery_nameplate_capacity_kwh": 40.0,
        "battery_effective_capacity_kwh": 40.0,
        # nimbus issue #1179: the blend-calibration outcome and the simplex
        # iteration count of the cycle that produced this plan -- i.e. of a
        # HEALTHY cycle, which is the base rate #1229/#1291 both argue makes
        # "the failures took the fallback" falsifiable at all.
        #
        # Every value here is a deterministic property of this fixture, not a
        # sample:
        #   - the calibrator accepts its FIRST probe at `_CAL_LOG_HI` (-1.0)
        #     and steps back by `_CAL_MARGIN` (1.0), so the weight is exactly
        #     10**-2. Not HiGHS-version sensitive: no bisection runs.
        #   - `False`/`None` because that acceptance means no fallback.
        #   - **`simplex_iterations: 0` on an OPTIMAL solve**, which is the
        #     single most useful thing this fixture now pins. #1179's
        #     hypothesis is that its 0.1s failures were rejected before the
        #     simplex loop, and the tempting read of a 0 there is "never
        #     iterated, therefore rejected". This fixture is a counter-example
        #     in the healthy direction: presolve finished it, so 0 iterations
        #     is a *successful* solve here. The count only discriminates
        #     against the same install's own baseline -- which is exactly why
        #     it is published rather than only logged on failure.
        "calibration_min_weight_fallback": False,
        "calibration_fallback_reason": None,
        "calibration_weight_used": 0.01,
        "simplex_iterations": 0,
    },
    "horizon_hours": 96.0,
    "solve_seconds": 0.0,  # excluded from comparison -- see test body
    "binding_constraint_now": "Grid export at zero (not economical right now)",
    # nimbus issue #662 (Mark Purcell): both regenerated from a fresh
    # real run after fixing the missing period_hours_arr[0] division --
    # power_balance_t0's own row dual (and the 4 variable-bound reduced
    # costs compute_binding_constraint_label() reads) come out of the LP
    # in "$ per kW of RHS/bound," carrying an implicit x hours[0] factor
    # relative to the true $/kWh marginal price, exactly the same
    # correction forced_import_cost already applied. A real, deliberate,
    # understood value change (both now ~12x larger, matching this
    # fixture's own 5-minute tier1 period at generation time), not
    # drift.
    # nimbus issue #696, Stage 2: regenerated alongside total_cost
    # above -- the CalibratedOptions searched blend weight shifts this
    # dual's own value by a tiny, real, understood amount too.
    # nimbus issue #732: regenerated alongside total_cost above.
    "binding_constraint_shadow_price": 0.2501,
    "energy_shadow_price_now": 0.3,
    "p2p_volume_cap_shadow_price": -0.0,
    # nimbus issue #567: this fixture configures no spike threshold/
    # alert entity -- honest, expected False, matching every other
    # "unconfigured means off" field in this fixture.
    "price_spike_active": False,
    "p2p_recent_avg_volume_kwh": 0.0,
    "load_forecast_source_used": f"single sensor: {_LOAD_SENSOR}",
    "load_forecast_source_error": None,
    "load_forecast_warnings": {},
    "load_forecast_coverage_hours": 1.0,
    "load_summed_18_now_kw": 1.5,
    "load_whole_house_cross_check_now_kw": None,
    # nimbus issue #429: new field, this fixture has no
    # solver_whole_house_cross_check_sensor configured so it's None,
    # same honest-absence shape as the sibling field above.
    "load_whole_house_live_now_kw": None,
    "failed_load_entities": [],
    "solar_delivery_ratio": None,
    "solar_delivery_sample_count": 0,
    "solar_delivery_underperforming": False,
    # nimbus 2026-09-07 (direct household ask): new fields exposing the
    # real raw-vs-risk-adjusted gap for period 0 -- see solver_writer.py's
    # own _risk_aversion_effect_now() docstring. This fixture configures
    # no solar/price confidence bands at all, so a genuine 0.0 (not None)
    # is the correct, honest result -- the mechanism ran, found nothing
    # to hedge against, same "zero is a real answer" distinction that
    # function's own docstring documents.
    "solar_risk_effect_now_kw": 0.0,
    "import_price_risk_effect_now": 0.0,
    "export_price_risk_effect_now": 0.0,
    # nimbus issue #493 (Signals 4/7 of #489, item 1): this fixture
    # configures no envelope entity, so both read as the plain static
    # solver_grid_max_import_kw/_export_kw values above (15.0) at every
    # period -- byte-identical to the pre-#493 behaviour.
    "envelope_import_limit_kw": 15.0,
    "envelope_export_limit_kw": 15.0,
}

# Per-period forecast keys asserted at fixed indices only (0, 1, -1) --
# all 202 periods in full would make a real, intentional future change
# to the grid shape (e.g. a tier-boundary tweak) needlessly tedious to
# update; three representative points (now, one step later, horizon end)
# already catch a real behavior change anywhere in the per-period
# construction just as reliably.
#
# nimbus issue #451: regenerated 2026-09-07 alongside the grid reshape
# above -- period 0/1 both sit inside the new, boundary-snapped tier1
# (5-min resolution still), period -1 is now index 201 (was 359),
# tier2 (30-min, was 1h).
_EXPECTED_FORECAST_SAMPLE = {
    0: {
        "battery_kw": -5.0,
        "grid_import_kw": 6.5,
        "grid_export_kw": 0.0,
        "solar_kw": 0.0,
        "load_kw": 1.5,
        "soc_pct": 56.02,
        "import_price": 0.3,
        "export_price": 0.05,
        # nimbus issue #662: direct per-period coverage of the same
        # period_hours_arr[i] correction verified at the top-level
        # energy_shadow_price_now (this period's own copy of the same
        # power_balance_t0 dual, same value by construction).
        "shadow_price": 0.3,
        # nimbus issue #493: no envelope entity configured in this
        # fixture -- flat static 15.0 at every period.
        "envelope_import_limit_kw": 15.0,
        "envelope_export_limit_kw": 15.0,
    },
    1: {
        "battery_kw": -5.0,
        "grid_import_kw": 6.5,
        "grid_export_kw": 0.0,
        "solar_kw": 0.0,
        "load_kw": 1.5,
        "soc_pct": 57.03,
        "import_price": 0.3,
        "export_price": 0.05,
        "shadow_price": 0.3,
        "envelope_import_limit_kw": 15.0,
        "envelope_export_limit_kw": 15.0,
    },
    -1: {
        "battery_kw": 1.3,
        "grid_import_kw": 0.0,
        "grid_export_kw": 0.0,
        "solar_kw": 0.0,
        "load_kw": 1.3,
        "soc_pct": 19.25,
        "import_price": 0.3,
        "export_price": 0.05,
        # A genuinely different value from period 0/1 above (a coarser,
        # later-horizon tier's own real duals) -- real, direct proof the
        # per-period division uses THAT period's own hours[i], not a
        # single shared/period-0 value applied everywhere.
        #
        # nimbus issue #696, Stage 2: regenerated alongside every other
        # value in this file after solve_options=CalibratedOptions()
        # became the real default -- same reasoning as this file's own
        # top-of-file comment on total_cost.
        # nimbus issue #732: regenerated again after the new
        # efficiency-loss cost changed this fixture's own real
        # dispatch.
        "shadow_price": 0.2982,
        "envelope_import_limit_kw": 15.0,
        "envelope_export_limit_kw": 15.0,
    },
}

# Keys deliberately excluded from the golden comparison because they are
# themselves still a re-derivation of the frozen "now" instant (a real
# timestamp/formatting difference here is not the kind of regression
# this guardrail exists to catch) or a wall-clock-measured duration.
# (Per-period `time` is handled separately -- _EXPECTED_FORECAST_SAMPLE
# below simply never lists it as a key to check.)
_TIME_DEPENDENT_ATTR_KEYS = {"generated_at", "solve_seconds"}


def _make_ha_get():
    known = {
        "sensor.nimbus_solver_config": {
            "state": "configured",
            "attributes": _SOLVER_CONFIG_ATTRS,
        },
        "sensor.fake_soc": {"state": "55.0", "attributes": {}},
        "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
        "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        _LOAD_SENSOR: _HEALTHY_LOAD_STATE,
    }

    def _ha_get(entity_id: str):
        if entity_id in known:
            return known[entity_id]
        raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

    return _ha_get


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
class TestMainGoldenOutput:
    def test_fixed_inputs_produce_the_exact_expected_plan(self, freezer):
        ha_get_mock = _make_ha_get()
        posted = {}

        def _capture_ha_post_state(entity_id, state, attrs=None):
            posted[entity_id] = (state, attrs)

        with (
            patch.object(solver_writer, "ha_get", side_effect=ha_get_mock),
            # nimbus #1302 (spec 003 step 2): safe_num moved to
            # solver_shared.py and resolves ha_get from THAT namespace, so
            # patching only solver_writer.ha_get lets its call escape to a
            # real HA. main()'s own reads still go through solver_writer's
            # alias, so BOTH targets are needed -- not one instead of the
            # other.
            patch.object(solver_shared, "ha_get", side_effect=ha_get_mock),
            patch.object(
                solver_writer, "ha_post_state", side_effect=_capture_ha_post_state
            ),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            # nimbus issue #1330: every persisted path into a throwaway
            # directory, so the golden comparison cannot inherit state from a
            # previous run or from the machine. See the module docstring, and
            # tests/_isolated_state.py for the full account.
            isolated_state_paths(solver_writer),
        ):
            solver_writer.main()

        assert solver_publish.ENTITY_ID in posted, (
            "main() did not push sensor.nimbus_solver_battery_forecast at all "
            "-- this fixture must reach a real optimal solve"
        )
        state, attrs = posted[solver_publish.ENTITY_ID]

        assert state == _EXPECTED_STATE

        forecast = attrs.get("forecast")
        assert forecast is not None and len(forecast) == _EXPECTED_ATTRS["n_periods"]

        # nimbus issue #563 item 5: same per-period, real-time-stamped
        # shape as "forecast" above -- checked structurally here (one
        # entry per real battery, each carrying the full per-period
        # series), not pinned into _EXPECTED_ATTRS's own byte-exact
        # comparison below, same reasoning as "forecast" itself.
        batteries = attrs.get("batteries")
        assert batteries is not None and len(batteries) == 1  # just "home" here
        assert batteries[0]["name"] == "home"
        assert len(batteries[0]["forecast"]) == _EXPECTED_ATTRS["n_periods"]
        assert set(batteries[0]["forecast"][0].keys()) == {
            "time",
            "charge_kw",
            "discharge_kw",
            "soc_kwh",
            "soc_pct",
        }

        actual_non_time = {
            k: v for k, v in attrs.items() if k not in _TIME_DEPENDENT_ATTR_KEYS
        }
        expected_non_time = {
            k: v
            for k, v in _EXPECTED_ATTRS.items()
            if k not in _TIME_DEPENDENT_ATTR_KEYS
        }
        actual_non_time.pop("forecast")
        actual_non_time.pop("batteries")
        # nimbus #1360: cycles since the process started, so their values
        # depend on how many solves this test process has already run.
        # Pinned by shape here; their values by the isolated golden master.
        diag = dict(actual_non_time["solve_diagnostics"])
        attempted = diag.pop("solves_attempted")
        failures = diag.pop("solver_failures")
        assert isinstance(attempted, int) and attempted >= 1
        assert isinstance(failures, int) and 0 <= failures <= attempted
        actual_non_time["solve_diagnostics"] = diag
        assert actual_non_time == expected_non_time, (
            "main()'s pushed attributes (excluding forecast/generated_at/"
            "solve_seconds) no longer match the golden output -- if this "
            "was a deliberate, understood behavior change, regenerate the "
            "golden values from a fresh real run; if this happened during "
            "a #363 extraction step, STOP -- the extraction changed real "
            "output and is not behavior-preserving."
        )

        for idx, expected_point in _EXPECTED_FORECAST_SAMPLE.items():
            actual_point = forecast[idx]
            for key, expected_value in expected_point.items():
                assert actual_point.get(key) == expected_value, (
                    f"forecast[{idx}]['{key}'] = {actual_point.get(key)!r}, "
                    f"expected {expected_value!r}"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
