"""Real test of diagnostics.py (2026-08-23) -- the async_get_config_entry_
diagnostics() Home Assistant calls for Settings -> Devices & Services ->
Nimbus -> Download diagnostics. Built in direct response to Mark Purcell
asking for exactly this ("Needs download diagnostic (gold tier) to fully
debug") to debug a real solver crash/flatline he hit (nimbus issues #63,
#66) on his own independent install.

Imports and exercises the REAL module (not a reimplementation) against
tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import diagnostics


def _fake_subentry(
    subentry_id: str, subentry_type: str, data: dict, title: str = "Test"
) -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = subentry_type
    s.data = data
    s.title = title
    return s


def _fake_coordinator(
    subentry, data: dict, last_update_success: bool = True
) -> MagicMock:
    c = MagicMock()
    c.subentry = subentry
    c.data = data
    c.last_update_success = last_update_success
    return c


def _fake_entry(coordinators: dict, options: dict, title: str = "Nimbus") -> MagicMock:
    entry = MagicMock()
    entry.title = title
    entry.options = options
    entry.runtime_data = coordinators
    return entry


def _fake_state(state, attributes: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state, attributes=attributes)


def test_solver_diagnostics_none_when_entity_never_pushed():
    hass = MagicMock()
    hass.states.get.return_value = None
    result = diagnostics._solver_diagnostics(hass)
    assert result == {"configured": False}


def test_solver_diagnostics_reads_real_health_fields_and_the_full_forecast_array():
    # 2026-08-24: reversed from the original "must NOT appear" version of
    # this test -- direct household + Mark Purcell instruction ("get more
    # data into the diagnostic file so we can actually understand the
    # reason its making decisions rather than just speculation without
    # any data to backup"). A downloaded diagnostics file has no 16384-
    # byte recorder-attribute constraint; there's no real reason to hold
    # the actual plan back from it.
    hass = MagicMock()
    real_forecast = [{"time": "t", "value": 1.0}] * 400
    real_load_forecast = [{"time": "t", "value": 2.0}] * 300

    def fake_get(entity_id):
        if entity_id == diagnostics._SOLVER_ENTITY_ID:
            return _fake_state(
                "13.4",
                {
                    "forecast": real_forecast,
                    "status": "optimal",
                    "generated_at": "2026-08-23T00:00:00+00:00",
                    "solve_seconds": 0.42,
                    "n_periods": 363,
                    "n_clamped_periods": 0,
                    "horizon_hours": 96.0,
                    "total_cost": -12.5,
                    "binding_constraint_now": "Battery max discharge power",
                    "load_forecast_source_error": None,
                    "failed_load_entities": [],
                    "load_summed_18_now_kw": 6.8,
                    "load_whole_house_cross_check_now_kw": 6.9,
                },
            )
        if entity_id == diagnostics._HOUSEHOLD_LOAD_ENTITY_ID:
            return _fake_state("6.8", {"forecast": real_load_forecast})
        return None

    hass.states.get.side_effect = fake_get
    result = diagnostics._solver_diagnostics(hass)

    assert result["configured"] is True
    assert result["entity_found"] is True
    assert result["status"] == "optimal"
    assert result["solve_seconds"] == 0.42
    assert result["n_periods"] == 363
    assert result["total_cost"] == -12.5
    assert result["binding_constraint_now"] == "Battery max discharge power"
    # The real, full arrays -- not a slice, not a summary, not omitted.
    assert result["forecast"] == real_forecast
    assert len(result["forecast"]) == 400
    assert result["household_load_forecast"] == real_load_forecast
    assert len(result["household_load_forecast"]) == 300


def test_solver_diagnostics_forecast_is_empty_list_not_missing_when_absent():
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: (
        _fake_state("0.0", {"status": "optimal"})
        if entity_id == diagnostics._SOLVER_ENTITY_ID
        else None
    )
    result = diagnostics._solver_diagnostics(hass)
    assert result["forecast"] == []
    assert result["household_load_forecast"] == []


def test_solver_diagnostics_surfaces_the_real_issue_66_failure_shape():
    # nimbus issue #66: a household's real load-forecast sensor had a
    # shape the writer script couldn't parse -- this field is exactly
    # what a diagnostics dump needs to show for that class of bug.
    hass = MagicMock()

    def fake_get(entity_id):
        if entity_id == diagnostics._SOLVER_ENTITY_ID:
            return _fake_state(
                "0.0",
                {
                    "status": "optimal",
                    "load_forecast_source_error": "sensor.emhass_deferrable0 has no 'forecast' attribute",
                    "failed_load_entities": ["sensor.emhass_deferrable0"],
                },
            )
        return None

    hass.states.get.side_effect = fake_get
    result = diagnostics._solver_diagnostics(hass)
    assert (
        result["load_forecast_source_error"]
        == "sensor.emhass_deferrable0 has no 'forecast' attribute"
    )
    assert result["load_failed_entities"] == ["sensor.emhass_deferrable0"]


def test_solver_diagnostics_spreads_every_attribute_not_a_curated_allowlist():
    # Nimbus issue #116 (Mark Purcell, 2026-08-25): cost_breakdown (v0.82
    # #149) and load_forecast_source_used (v0.83 #148) both reach the
    # live entity correctly but stayed null on this diagnostic because
    # the old hand-picked allowlist here was never extended to include
    # them. Fixed by spreading the entity's full attribute dict instead
    # of naming fields one at a time -- this proves ANY attribute
    # (including ones that don't exist yet) survives into the diagnostic
    # dump untouched, closing the whole class of bug rather than just
    # these two fields.
    hass = MagicMock()

    def fake_get(entity_id):
        if entity_id == diagnostics._SOLVER_ENTITY_ID:
            return _fake_state(
                "13.4",
                {
                    "status": "optimal",
                    "cost_breakdown": {"grid_net": -15.4797, "degradation": 5.331},
                    "load_forecast_source_used": "single sensor: sensor.x",
                    "energy_shadow_price_now": 0.31,
                    "total_charge_kwh": 12.5,
                    "total_discharge_kwh": 9.2,
                    "load_forecast_warnings": {},
                    "load_forecast_coverage_hours": None,
                    "some_brand_new_field_added_later": "still shows up",
                },
            )
        return None

    hass.states.get.side_effect = fake_get
    result = diagnostics._solver_diagnostics(hass)

    assert result["cost_breakdown"] == {"grid_net": -15.4797, "degradation": 5.331}
    assert result["load_forecast_source_used"] == "single sensor: sensor.x"
    assert result["energy_shadow_price_now"] == 0.31
    assert result["total_charge_kwh"] == 12.5
    assert result["total_discharge_kwh"] == 9.2
    assert result["load_forecast_warnings"] == {}
    assert result["load_forecast_coverage_hours"] is None
    assert result["some_brand_new_field_added_later"] == "still shows up"


def test_get_config_entry_diagnostics_groups_subentries_and_includes_solver():
    ps = _fake_subentry(
        "ps1", "power_source", {"power_source_name": "Inverter 1"}, title="Inverter 1"
    )
    real_forecast = [{"time": "t", "value": 1.0}]
    coordinators = {
        "ps1": _fake_coordinator(ps, {"forecast": real_forecast, "trained_at": "x"})
    }
    entry = _fake_entry(
        coordinators, {"switchboard_grid_meter_sensor": "sensor.grid_meter"}
    )

    hass = MagicMock()
    hass.states.get.return_value = None  # Solver never configured for this fake entry

    import asyncio

    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert result["entry"]["title"] == "Nimbus"
    assert (
        result["entry"]["options"]["switchboard_grid_meter_sensor"]
        == "sensor.grid_meter"
    )
    assert len(result["subentries"]) == 1
    assert result["subentries"][0]["subentry_id"] == "ps1"
    assert result["subentries"][0]["coordinator"]["forecast_point_count"] == 1
    # 2026-08-24: subentries now carry their own full real forecast too,
    # not just the point count.
    assert result["subentries"][0]["coordinator"]["forecast"] == real_forecast
    assert result["solver"] == {"configured": False}
    assert result["solver_config"] == {"configured": False}


def test_get_config_entry_diagnostics_surfaces_model_type():
    # Nimbus issue #196 (Mark Purcell): PR #193 fixed model selection across
    # {knn, gbrt, naive}, but the diagnostic gave no way to see WHICH model
    # actually deployed -- validation_mae shows the inputs to the decision,
    # never the outcome. Confirms model_type survives into the per-subentry
    # diagnostic dump, both when set and when absent (a pre-#196 coordinator
    # tick, or an untrained coordinator).
    ps = _fake_subentry("ps1", "power_source", {}, title="Battery")
    coordinators = {
        "ps1": _fake_coordinator(
            ps, {"forecast": [], "trained_at": "x", "model_type": "naive"}
        ),
        "ps2": _fake_coordinator(
            _fake_subentry("ps2", "power_source", {}, title="Grid"),
            {"forecast": [], "trained_at": None},  # no model_type key at all
        ),
    }
    entry = _fake_entry(coordinators, {})
    hass = MagicMock()
    hass.states.get.return_value = None

    import asyncio

    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    by_id = {s["subentry_id"]: s for s in result["subentries"]}
    assert by_id["ps1"]["coordinator"]["model_type"] == "naive"
    assert by_id["ps2"]["coordinator"]["model_type"] is None


def test_solver_config_diagnostics_none_when_entity_never_pushed():
    hass = MagicMock()
    hass.states.get.return_value = None
    result = diagnostics._solver_config_diagnostics(hass)
    assert result == {"configured": False}


def test_solver_config_diagnostics_includes_every_field_incl_pre_set_defaults():
    # Direct household instruction (2026-08-24): "diagnostics must have
    # everything in it incl pre-set values." sensor.nimbus_solver_config's
    # own extra_state_attributes already resolves EVERY _SOLVER_ALL_KEYS
    # field (including a number.nimbus_solver_* entity's own default
    # value if it was never touched) -- this proves the diagnostics dump
    # passes all of it through verbatim, nothing curated or dropped.
    hass = MagicMock()
    full_config = {
        "solver_battery_capacity_kwh": 40.0,
        "solver_battery_min_soc_percent": 5.0,  # a real pre-set default
        "solver_max_charge_kw": 21.0,
        "solver_max_discharge_kw": 24.0,
        "solver_max_discharge_live_entity": None,
        "solver_import_price_risk_aversion": 0.0,  # a real pre-set default
        "solver_export_price_risk_aversion": 0.0,
        "solver_p2p_bonus_price": 0.0,
        "unresolved_required_keys": [],
    }
    hass.states.get.side_effect = lambda entity_id: (
        _fake_state("configured", full_config)
        if entity_id == diagnostics._SOLVER_CONFIG_ENTITY_ID
        else None
    )

    result = diagnostics._solver_config_diagnostics(hass)

    assert result["configured"] is True
    assert result["native_value"] == "configured"
    # Every single field, verbatim -- not a subset.
    for key, value in full_config.items():
        assert result[key] == value


def test_solver_config_diagnostics_surfaces_unresolved_required_keys():
    # Real diagnostic value on issue #85 (see NimbusSolverConfigSensor's
    # own docstring): this list is exactly what tells a caller WHY the
    # Solver is reporting "unconfigured" without needing HA's own logs.
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: (
        _fake_state(
            "unconfigured",
            {"unresolved_required_keys": ["solver_battery_capacity_kwh"]},
        )
        if entity_id == diagnostics._SOLVER_CONFIG_ENTITY_ID
        else None
    )
    result = diagnostics._solver_config_diagnostics(hass)
    assert result["native_value"] == "unconfigured"
    assert result["unresolved_required_keys"] == ["solver_battery_capacity_kwh"]


def test_get_config_entry_diagnostics_redacts_configured_fields():
    # TO_REDACT is deliberately empty today (see diagnostics.py's own
    # module docstring) -- this proves the PATH still works correctly
    # for a hypothetical future sensitive field, so it doesn't need
    # retrofitting the day one is actually added.
    original = diagnostics.TO_REDACT
    diagnostics.TO_REDACT = ("secret_token",)
    try:
        entry = _fake_entry({}, {"secret_token": "abc123", "safe_field": "visible"})
        hass = MagicMock()
        hass.states.get.return_value = None
        import asyncio

        result = asyncio.run(
            diagnostics.async_get_config_entry_diagnostics(hass, entry)
        )
        assert result["entry"]["options"]["secret_token"] == "**REDACTED**"
        assert result["entry"]["options"]["safe_field"] == "visible"
    finally:
        diagnostics.TO_REDACT = original
