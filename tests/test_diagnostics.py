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


def test_solver_diagnostics_reads_real_health_fields_not_the_forecast_array():
    hass = MagicMock()

    def fake_get(entity_id):
        if entity_id == diagnostics._SOLVER_ENTITY_ID:
            return _fake_state(
                "13.4",
                {
                    "forecast": [{"time": "t", "value": 1.0}]
                    * 400,  # must NOT appear below
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
            return _fake_state("6.8", {})
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
    assert "forecast" not in result


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


def test_get_config_entry_diagnostics_groups_subentries_and_includes_solver():
    ps = _fake_subentry(
        "ps1", "power_source", {"power_source_name": "Inverter 1"}, title="Inverter 1"
    )
    coordinators = {
        "ps1": _fake_coordinator(ps, {"forecast": [{"time": "t"}], "trained_at": "x"})
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
    assert result["solver"] == {"configured": False}


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
