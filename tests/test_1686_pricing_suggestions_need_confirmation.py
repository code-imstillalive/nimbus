"""nimbus #1686 (Mark Purcell, 10 Oct 2026): discovering a price integration
must never change the saved pricing configuration.

Before this, the Solver settings wizard pre-filled empty price fields from
detected integrations (#1550 LocalVolts v2, #1580 Amber Express, #1579 Amber
Electric, #1578 the regional spot forecast) and from the Energy Dashboard
(#1067). It also replaced a set field whose entity had no state at that
moment. So submitting the wizard for any reason (a battery number, say)
saved the suggestions, and an entity briefly missing during startup or a
reload lost its binding. The startup notification said "Nothing has been
changed", which was true until the next save.

Now:
- the wizard's forms show saved values only;
- a set field is never proposed for, even when its entity is missing;
- suggestions are listed under Configure -> Review pricing suggestions and
  applied only when the household submits that step, to fields still empty
  at that moment, each logged and listed in a notification.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import pricing_autodetect as pa
from custom_components.nimbus_load.const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
)
from custom_components.nimbus_load.flows import hub_options as ho

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

LV = {
    CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.localvolts_v2_buy_flex_up",
    CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.localvolts_v2_sell_flex_up",
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.localvolts_v2_flex_up_forecast",
}
PD7 = {CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "sensor.qld1_pd7day_forecast"}
AMBER = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.amber_general_price"}


def _detectors(lv=LV, amber=None, pd7=PD7):
    """Every detector patched, so these tests exercise the merge rules and
    the flow, not the registry matching (that has its own tests)."""
    return (
        patch.object(pa, "detect_localvolts_v2_profile", lambda _h: dict(lv)),
        patch.object(pa, "detect_amber_express_profile", lambda _h: dict(amber or {})),
        patch.object(pa, "detect_amber_core_profile", lambda _h: {}),
        patch.object(
            pa,
            "detect_regional_spot_forecast",
            lambda _h: (dict(pd7), "NEM PD7DAY") if pd7 else ({}, None),
        ),
    )


def _with(patches, fn):
    with patches[0], patches[1], patches[2], patches[3]:
        return fn()


def _flow(options):
    flow = ho.NimbusHubOptionsFlow.__new__(ho.NimbusHubOptionsFlow)
    flow.hass = MagicMock()
    flow.hass.services.async_call = AsyncMock()
    entry = SimpleNamespace(options=dict(options))
    try:
        flow.config_entry = entry
    except AttributeError:
        flow._config_entry = entry
    flow._solver_data = {}
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_show_menu = MagicMock(side_effect=lambda **kw: {"type": "menu", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "create_entry", **kw}
    )
    flow.async_abort = MagicMock(side_effect=lambda **kw: {"type": "abort", **kw})
    return flow


def _ed(prices=None):
    return patch.object(
        ho,
        "_energy_dashboard_solver_source_suggestions",
        AsyncMock(return_value=dict(prices or {})),
    )


# ---- the merge rules --------------------------------------------------------


def test_a_complete_manual_configuration_gets_no_suggestions():
    saved = {
        **AMBER,
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.amber_feed_in",
        CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.my_array",
        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "",
    }
    got = _with(_detectors(), lambda: pa.pricing_suggestions(MagicMock(), saved))
    # Only the genuinely empty regional field is proposed; every set field,
    # including one from another integration, is left out.
    assert [s["field"] for s in got] == [CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR]
    assert got[0]["current"] is None and got[0]["source"] == "NEM PD7DAY"


def test_a_set_field_whose_entity_is_missing_is_never_proposed_for():
    saved = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.reloading_right_now"}
    got = _with(_detectors(), lambda: pa.pricing_suggestions(MagicMock(), saved))
    assert CONF_SOLVER_IMPORT_PRICE_SENSOR not in {s["field"] for s in got}
    filled = _with(_detectors(), lambda: pa.with_detected_profile(MagicMock(), saved))
    assert filled[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.reloading_right_now"


def test_energy_dashboard_prices_come_first_and_are_labelled():
    got = _with(
        _detectors(),
        lambda: pa.pricing_suggestions(
            MagicMock(), {}, {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.ed_import"}
        ),
    )
    by_field = {s["field"]: s for s in got}
    assert by_field[CONF_SOLVER_IMPORT_PRICE_SENSOR]["proposed"] == "sensor.ed_import"
    assert by_field[CONF_SOLVER_IMPORT_PRICE_SENSOR]["source"] == "Energy Dashboard"
    assert by_field[CONF_SOLVER_EXPORT_PRICE_SENSOR]["source"] == "LocalVolts v2"


def test_apply_changes_only_fields_still_empty():
    suggestions = [
        {
            "field": CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
            "proposed": "sensor.pd7",
            "label": "Regional spot forecast",
            "current": None,
            "source": "NEM PD7DAY",
        },
        {
            "field": CONF_SOLVER_IMPORT_PRICE_SENSOR,
            "proposed": "sensor.lv_buy",
            "label": "Import price",
            "current": None,
            "source": "LocalVolts v2",
        },
    ]
    # The import price was set after the list was shown: it must survive.
    options = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.chosen_since", "other": 1}
    merged, applied = pa.apply_pricing_suggestions(options, suggestions)
    assert merged[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.chosen_since"
    assert merged[CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR] == "sensor.pd7"
    assert merged["other"] == 1
    assert [a["field"] for a in applied] == [CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR]


# ---- the wizard never carries a suggestion ---------------------------------


def _captured_defaults(step, options):
    flow = _flow(options)
    seen = {}

    def grab(name):
        real = getattr(ho, name)

        def wrapper(defaults, *a, **kw):
            seen[name] = dict(defaults)
            return real(defaults, *a, **kw)

        return wrapper

    with (
        patch.object(ho, "_solver_grid_schema", grab("_solver_grid_schema")),
        patch.object(ho, "_solver_sources_schema", grab("_solver_sources_schema")),
        patch.object(
            ho, "_discover_nimbus_load_forecast_candidates", lambda _h: ([], [])
        ),
        _ed({CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.ed_import"}),
    ):
        _with(_detectors(), lambda: asyncio.run(getattr(flow, step)(None)))
    return seen


def test_the_grid_step_shows_saved_values_only():
    saved = {CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.my_export"}
    seen = _captured_defaults("async_step_solver_grid", saved)
    defaults = seen["_solver_grid_schema"]
    assert not defaults.get(CONF_SOLVER_IMPORT_PRICE_SENSOR)  # empty stays empty
    assert defaults[CONF_SOLVER_EXPORT_PRICE_SENSOR] == "sensor.my_export"


def test_the_sources_step_shows_saved_values_only_and_a_blank_continuation_stays_blank():
    saved = {CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.my_array"}
    seen = _captured_defaults("async_step_solver_sources", saved)
    defaults = seen["_solver_sources_schema"]
    assert not defaults.get(CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR)
    assert defaults == saved


def test_saving_the_wizard_unchanged_commits_no_suggestion():
    """Walk all three Solver steps, submitting exactly what each form shows,
    as a household changing only a battery number would. The saved pricing
    must come out as it went in."""
    saved = {CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.my_array"}
    flow = _flow(saved)
    flow._solver_data = None  # seeded from the saved options, as in a real run
    # The grid form shows saved values only, and the saved import/export are
    # empty, so an unchanged submission of that step carries no price field.
    with (
        patch.object(
            ho, "_discover_nimbus_load_forecast_candidates", lambda _h: ([], [])
        ),
        _ed({CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.ed_import"}),
        patch.object(ho, "frozen_price_field_errors", lambda *_a, **_k: {}),
        patch.object(ho.NimbusHubOptionsFlow, "async_step_solver_sources") as sources,
    ):
        sources.return_value = {"type": "next"}
        _with(
            _detectors(),
            lambda: asyncio.run(flow.async_step_solver_grid({})),
        )
    for field in (
        CONF_SOLVER_IMPORT_PRICE_SENSOR,
        CONF_SOLVER_EXPORT_PRICE_SENSOR,
        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
    ):
        assert not flow._solver_data.get(field), field
    assert (
        flow._solver_data[CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR] == "sensor.my_array"
    )


# ---- the review step -------------------------------------------------------


def test_the_menu_offers_review_only_when_there_is_something_to_review():
    full = {**LV, **PD7}
    flow = _flow(full)
    with _ed():
        menu = _with(_detectors(), lambda: asyncio.run(flow.async_step_init()))
    assert "pricing_suggestions" not in menu["menu_options"]
    flow = _flow({})
    with _ed():
        menu = _with(_detectors(), lambda: asyncio.run(flow.async_step_init()))
    assert "pricing_suggestions" in menu["menu_options"]


def test_the_review_form_lists_each_change_and_saves_nothing_until_submitted():
    flow = _flow({CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.my_array"})
    with _ed():
        shown = _with(
            _detectors(), lambda: asyncio.run(flow.async_step_pricing_suggestions())
        )
    assert shown["type"] == "form"
    listed = shown["description_placeholders"]["suggestions"]
    assert "sensor.qld1_pd7day_forecast" in listed and "NEM PD7DAY" in listed
    flow.async_create_entry.assert_not_called()


def test_submitting_the_review_applies_logs_and_notifies():
    flow = _flow({CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "sensor.my_array"})
    with _ed(), patch.object(ho._LOGGER, "warning") as warn:
        done = _with(
            _detectors(),
            lambda: asyncio.run(flow.async_step_pricing_suggestions({})),
        )
    assert done["type"] == "create_entry"
    data = done["data"]
    assert (
        data[CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR] == "sensor.qld1_pd7day_forecast"
    )
    assert data[CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR] == "sensor.my_array"
    assert warn.call_count >= 1
    note = flow.hass.services.async_call.call_args.args[2]
    assert "sensor.qld1_pd7day_forecast" in note["message"]


def test_nothing_to_review_aborts_cleanly():
    flow = _flow({**LV, **PD7})
    with _ed():
        done = _with(
            _detectors(), lambda: asyncio.run(flow.async_step_pricing_suggestions())
        )
    assert done == {"type": "abort", "reason": "no_pricing_suggestions"}


# ---- wording ---------------------------------------------------------------


def test_no_notification_or_text_still_says_pre_filled_and_saved():
    src = (ROOT / "pricing_autodetect.py").read_text(encoding="utf-8")
    assert "for you to check and save" not in src
    assert "pre-filled there" not in src
    assert "pre-filled in their place" not in src
    for path in (ROOT / "strings.json", ROOT / "translations" / "en.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        opts = data["options"]
        assert "pricing_suggestions" in opts["step"]["init"]["menu_options"], path
        step = opts["step"]["pricing_suggestions"]
        assert "{suggestions}" in step["description"], path
        assert opts["abort"]["no_pricing_suggestions"], path
