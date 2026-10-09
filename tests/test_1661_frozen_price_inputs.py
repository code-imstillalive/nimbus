"""nimbus #1661 (Mark Purcell, 9 Oct 2026): price inputs must be live.

LocalVolts' Rate All Var (`amountVar / volume`, with `amountVar` and
`amountAll` behind it) is written when the forecast for an interval is built
and is not revised when it settles. On 9 Oct it stayed near 13c export while
Flex Up and spot followed a $1.32/kWh spike. A price field pointed at one
removes the spike from the solve without an error, so it is refused in the
Grid step and reported by a Repair, and no solver price input may be built
from those fields at all.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import price_input_check as pic
from custom_components.nimbus_load import setup_health as sh
from custom_components.nimbus_load.flows import hub_options as ho

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

ATTRS = {
    "sensor.localvolts_v2_buy_flex_up": {"source_field": "flexUp"},
    "sensor.localvolts_v2_sell_flex_up": {"source_field": "flexUp"},
    "sensor.localvolts_v2_buy_rate_all_var": {"source_field": "rateAllVar"},
    "sensor.lv_sell_amount_var": {"source_field": "amountVar"},
    "sensor.lv_amount_all": {"source_field": "amountAll"},
    "sensor.renamed_export_rate": {},
}


def _hass(attrs=ATTRS) -> MagicMock:
    hass = MagicMock()
    hass.states.get.side_effect = lambda e: (
        SimpleNamespace(entity_id=e, state="0.3", attributes=attrs[e])
        if e in attrs
        else None
    )
    return hass


def _no_registry(_hass, _entity_id):
    return None


# --- recognising a frozen rate -----------------------------------------------


def test_each_frozen_source_field_is_recognised():
    with patch.object(pic, "_unique_id", _no_registry):
        for eid in (
            "sensor.localvolts_v2_buy_rate_all_var",
            "sensor.lv_sell_amount_var",
            "sensor.lv_amount_all",
        ):
            assert pic.frozen_rate_field(_hass(), eid) is not None, eid


def test_flex_up_is_live_and_accepted():
    with patch.object(pic, "_unique_id", _no_registry):
        assert (
            pic.frozen_rate_field(_hass(), "sensor.localvolts_v2_buy_flex_up") is None
        )


def test_a_renamed_entity_is_caught_by_its_unique_id():
    with patch.object(pic, "_unique_id", lambda _h, _e: "01abc_sell_rate_all_var"):
        why = pic.frozen_rate_field(_hass(), "sensor.renamed_export_rate")
    assert why == "unique id ends _rate_all_var"


def test_a_missing_entity_is_not_known_to_be_wrong():
    with patch.object(pic, "_unique_id", _no_registry):
        assert pic.frozen_rate_field(_hass(), "sensor.not_loaded_yet") is None
        assert pic.frozen_rate_field(_hass(), None) is None


def test_only_frozen_price_fields_are_reported():
    options = {
        "solver_import_price_sensor": "sensor.localvolts_v2_buy_flex_up",
        "solver_export_price_sensor": "sensor.lv_sell_amount_var",
        "solver_import_price_sensor_2": "sensor.localvolts_v2_buy_rate_all_var",
        "solver_battery_soc_sensor": "sensor.lv_amount_all",  # not a price field
    }
    with patch.object(pic, "_unique_id", _no_registry):
        found = pic.find_frozen_price_inputs(_hass(), options)
    assert sorted(label for label, _e, _w in found) == [
        "Solver settings: export price",
        "Solver settings: import price (second source)",
    ]


# --- the Repair ---------------------------------------------------------------


def test_the_repair_names_each_frozen_input():
    issues = sh.evaluate_health(
        options={
            "solver_battery_soc_sensor": "sensor.soc",
            "solver_load_forecast_sensor": "sensor.load_fc",
            "solver_solar_forecast_sensor": "sensor.pv_fc",
            "solver_import_price_sensor": "sensor.buy",
            "solver_export_price_sensor": "sensor.sell",
        },
        forecasts=[],
        hub_up_for=sh.TRAIN_GRACE,
        frozen_price_inputs=[
            ("Solver settings: export price", "sensor.sell", "source_field rateAllVar")
        ],
    )
    assert [i.kind for i in issues] == [sh.KIND_FROZEN_PRICE]
    assert issues[0].placeholders["inputs"] == (
        "- Solver settings: export price: `sensor.sell` (source_field rateAllVar)"
    )


def test_the_repair_and_the_form_error_have_text_in_every_translation():
    for path in (ROOT / "strings.json", ROOT / "translations" / "en.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        issue = data["issues"][sh.KIND_FROZEN_PRICE]
        assert issue["title"] and "{inputs}" in issue["description"], path
        assert "Flex Up" in data["options"]["error"]["frozen_price_signal"], path


# --- the Grid step -------------------------------------------------------------


def _grid_flow(hass):
    flow = ho.NimbusHubOptionsFlow.__new__(ho.NimbusHubOptionsFlow)
    flow.hass = hass
    entry = SimpleNamespace(options={})
    try:
        flow.config_entry = entry
    except AttributeError:
        flow._config_entry = entry
    flow._solver_data = {}
    flow.async_step_solver_sources = AsyncMock(return_value={"type": "next"})
    return flow


def _run_grid(flow, user_input):
    with (
        patch.object(pic, "_unique_id", _no_registry),
        patch.object(
            ho,
            "_energy_dashboard_solver_source_suggestions",
            AsyncMock(return_value={}),
        ),
        patch.object(ho, "with_detected_profile", lambda _h, d: d),
    ):
        return asyncio.run(flow.async_step_solver_grid(user_input))


def test_the_grid_step_refuses_a_frozen_export_price():
    flow = _grid_flow(_hass())
    result = _run_grid(
        flow,
        {
            "solver_import_price_sensor": "sensor.localvolts_v2_buy_flex_up",
            "solver_export_price_sensor": "sensor.localvolts_v2_buy_rate_all_var",
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"solver_export_price_sensor": "frozen_price_signal"}
    flow.async_step_solver_sources.assert_not_called()


def test_the_grid_step_accepts_live_prices():
    flow = _grid_flow(_hass())
    result = _run_grid(
        flow,
        {
            "solver_import_price_sensor": "sensor.localvolts_v2_buy_flex_up",
            "solver_export_price_sensor": "sensor.localvolts_v2_sell_flex_up",
        },
    )
    assert result == {"type": "next"}


# --- regression: no solver price input is built from a frozen field ------------


def test_no_solver_code_reads_a_frozen_field():
    """Acceptance 3 of #1661. Only price_input_check.py, which exists to
    recognise these fields, may name them."""
    pattern = re.compile(r"rateAllVar|amountVar|amountAll")
    offenders = [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.py")
        if p.name != "price_input_check.py"
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
