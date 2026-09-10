"""Real regression test for issue #99: NimbusForecastSensor (the
subentry-published load/signal forecast class) was missed by PR #77's
Recorder 16 KB attribute-cap fix -- that PR added
`_unrecorded_attributes = frozenset({"forecast"})` to
_NimbusSolverPushSensor (see test_sensor_solver_push_entities.py's own
matching class-attribute checks) but never to this class, so any
subentry's own real forecast sensor (a household's 18 loads, or Mark
Purcell's own confirmed-live #99 report showing this warning firing
hundreds of times for his signal forecasts) kept tripping the
Recorder's "exceeds maximum size of 16384 bytes" warning #77 was meant
to close everywhere.

Class-attribute check only, same minimal pattern as the sibling test
file's own -- no instance construction needed, _unrecorded_attributes
is read directly off the class.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import load_run_state, sensor


def test_forecast_sensor_has_unrecorded_attributes_for_recorder_cap():
    cls = sensor.NimbusForecastSensor
    # Recorder 16 KB attribute cap fix (#59/#99): the forecast list is a
    # projection, not a historical fact, so it's excluded from
    # long-term storage -- same reasoning, same fix, as the solver-push
    # sensors already carry.
    assert cls._unrecorded_attributes == frozenset({"forecast"})


def test_health_report_sensor_has_unrecorded_attributes_for_recorder_cap():
    # Same class of bug as #99 above, found live on devhub (2026-09-01):
    # NimbusHealthReportSensor's extra_state_attributes() returns up to
    # 20 recent_errors + 20 recent_warnings + one subentry_status entry
    # per forecastable subentry -- real, current diagnostic state, not
    # a historical fact worth keeping in long-term stats, and large
    # enough to trip the Recorder's 16 KB cap on nearly every cycle
    # (confirmed live: 62 warnings in one 30-minute window). This class
    # was never given the same _unrecorded_attributes treatment #99
    # gave NimbusForecastSensor -- fixed the same way.
    #
    # nimbus issue #362 (Mark Purcell): generated_at ALSO needs to be
    # here. Size was never the actual problem -- churn was. This entity
    # polls every 30s and generated_at is recomputed fresh on every
    # single read, so the attribute dict differed on every poll (firing
    # a non-dedupable recorder write) even when the three keys above
    # were byte-for-byte unchanged.
    cls = sensor.NimbusHealthReportSensor
    assert cls._unrecorded_attributes == frozenset(
        {"recent_errors", "recent_warnings", "subentry_status", "generated_at"}
    )


def test_quality_report_sensor_excludes_its_oversized_hourly_arrays():
    # nimbus issue #362 (Mark Purcell): NimbusSolverQualityReportSensor
    # cleared _unrecorded_attributes to frozenset() reasoning only about
    # the inherited "forecast" key -- but its real payload (see
    # solver_writer.py's publish_daily_quality_report()) also carries
    # j_ref_hourly/j_ach_hourly/j_star_hourly (24 ISO-keyed rows x 7
    # floats each) plus hourly_regret. A realistic payload measured at
    # 14,609 bytes against the recorder's 16,384-byte MAX_STATE_ATTRS_
    # BYTES cap -- one more trajectory or per-hour field tips it over,
    # at which point the recorder drops the ENTIRE attribute dict. The
    # flattened children already exclude exactly these four keys
    # (sensor_flattened.py's own FLATTENED_ATTRS_QUALITY) -- the parent
    # now matches. nimbus issue #681 added a fifth oversized field,
    # soc_discrepancy_hourly (one row per real recorder hour compared),
    # for the same reason.
    cls = sensor.NimbusSolverQualityReportSensor
    assert cls._unrecorded_attributes == frozenset(
        {
            "j_ref_hourly",
            "j_ach_hourly",
            "j_star_hourly",
            "hourly_regret",
            "soc_discrepancy_hourly",
        }
    )


def test_commanded_state_sensor_has_unrecorded_attributes_for_recorder_cap():
    # nimbus issue #625 (Mark Purcell, real finding: "State attributes...
    # exceed maximum size of 16384 bytes" firing every solve since
    # v0.94.195 landed -- when the recorder hits this cap it drops the
    # WHOLE state row's attributes, not just the oversized series, so
    # delivered_today_kwh/activations_today/cost_today/last_idle_
    # temperature and everything else in this row's history went with
    # it too). plan_cost_forecast (#591, v0.94.193) and temperature_
    # forecast (#592, v0.94.195) were both added to LoadRunState without
    # ever being added here.
    # nimbus issue #613 (item 3 of 3): plan_status_reason joins the same
    # set -- it refreshes every solve cycle exactly like its siblings
    # here, same reasoning.
    cls = sensor.NimbusControllableLoadStateSensor
    assert cls._unrecorded_attributes == frozenset(
        {
            "plan_forecast",
            "plan_delivered_kwh_forecast",
            "plan_status_reason",
            "plan_target_kwh",
            "plan_shortfall_kwh",
            "plan_earliest_period",
            "plan_deadline_period",
            "plan_nominal_kw",
            "plan_cost_forecast",
            "plan_shadow_price_forecast",
            "temperature_forecast",
        }
    )


def test_commanded_state_sensor_excludes_every_list_valued_load_run_state_field():
    # nimbus issue #625's own suggested fix: a fixed-set check (like the
    # one directly above) only catches a REGRESSION of an already-known
    # field -- it says nothing about the NEXT new series (there have
    # been four just this week: plan_cost_forecast, temperature_
    # forecast, plan_shadow_price_forecast, and the one this fix adds
    # coverage for). This instead introspects LoadRunState's own field
    # list directly: any field whose type annotation is `list[...]`
    # publishes a real per-period series onto this sensor (via
    # LoadRunState.to_dict()'s own full spread, sensor.py's async_
    # update()) and MUST be excluded from the recorder, or this test
    # fails the moment a new one is added without also updating
    # _unrecorded_attributes -- closing the whole class of bug #625 was,
    # not just today's two missing fields.
    list_valued_fields = {
        f.name
        for f in dataclasses.fields(load_run_state.LoadRunState)
        if f.type.startswith("list[")
    }
    assert list_valued_fields, "sanity check: expected at least one list field"
    missing = (
        list_valued_fields
        - sensor.NimbusControllableLoadStateSensor._unrecorded_attributes
    )
    assert not missing, (
        f"{missing} are list-valued LoadRunState fields not excluded from "
        "the recorder on NimbusControllableLoadStateSensor -- add them to "
        "_unrecorded_attributes (nimbus issue #625)"
    )
