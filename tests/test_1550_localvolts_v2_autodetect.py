"""nimbus #1550: detect LocalVolts v2 and propose the Solver's pricing profile.

A tester set the P2P matched-rate sensor by hand but not the price forecast
array, and his plan showed no P2P at all: without the array Nimbus prices on
its generic path, which never reads the matched rate. Detection pre-fills the
five LocalVolts v2 fields, only where empty, and says so at startup; it never
overwrites a field the household has set.

Sensors are matched by integration + unique_id (`<entry id>_<key>`), which is
what the LV v2 code sets (sensor.py / haeo_feed.py / paired_feed.py /
p2p_history_sensor.py) and what the reference household's registry holds.
"""

from __future__ import annotations

import asyncio
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
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR,
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2,
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
)

ENTRY = "01KZN6XV8PWEE360AKDHZ2XH1S"  # the reference household's LV v2 entry
FULL = {
    "buy_flex_up": "sensor.localvolts_v2_buy_flex_up",
    "sell_flex_up": "sensor.localvolts_v2_sell_flex_up",
    "flex_up_forecast": "sensor.localvolts_v2_flex_up_forecast",
    "current_sell_rate": "sensor.localvolts_v2_current_sell_rate",
    "p2p_settlement_history": "sensor.localvolts_v2_p2p_settlement_history",
    # nimbus #1537: the second matched-rate source (haeo_feed.py key).
    "sell_matched_cost": "sensor.localvolts_v2_sell_p2p_matched_cost",
}


class _Registry:
    def __init__(self, keys, disabled=()):
        self._by_uid = {
            ("sensor", "localvolts_v2", f"{ENTRY}_{k}"): eid for k, eid in keys.items()
        }
        self._disabled = {keys[k] for k in disabled}

    def async_get_entity_id(self, domain, platform, unique_id):
        return self._by_uid.get((domain, platform, unique_id))

    def async_get(self, entity_id):
        return SimpleNamespace(
            disabled_by="user" if entity_id in self._disabled else None
        )


def _hass(entries=(("loaded", ENTRY),)):
    hass = MagicMock()
    hass.config_entries.async_entries = MagicMock(
        return_value=[
            SimpleNamespace(state=SimpleNamespace(value=st), entry_id=eid)
            for st, eid in entries
        ]
    )
    hass.services.async_call = AsyncMock()
    # Every entity the tests name exists, unless a test says otherwise.
    hass.states.get = MagicMock(side_effect=lambda eid: object())
    return hass


def _detect(hass, keys=FULL, disabled=()):
    with patch.object(pa.er, "async_get", return_value=_Registry(keys, disabled)):
        return pa.detect_localvolts_v2_profile(hass)


def test_full_lv_v2_install_gives_the_six_field_profile():
    assert _detect(_hass()) == {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: FULL["buy_flex_up"],
        CONF_SOLVER_EXPORT_PRICE_SENSOR: FULL["sell_flex_up"],
        CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: FULL["flex_up_forecast"],
        CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR: FULL["current_sell_rate"],
        CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR: FULL["p2p_settlement_history"],
        CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2: FULL["sell_matched_cost"],
    }


def test_matching_is_by_unique_id_not_entity_id():
    """A renamed entity (the reference household's came up as
    sensor.hallway_zone_...) is still found by its unique_id."""
    renamed = {
        **FULL,
        "flex_up_forecast": "sensor.hallway_zone_localvolts_v2_flex_up_forecast",
    }
    found = _detect(_hass(), renamed)
    assert found[CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR] == renamed["flex_up_forecast"]


def test_an_older_lv_v2_omits_what_it_does_not_have():
    older = {
        k: v
        for k, v in FULL.items()
        if k not in ("sell_flex_up", "flex_up_forecast", "sell_matched_cost")
    }
    found = _detect(_hass(), older)
    assert CONF_SOLVER_EXPORT_PRICE_SENSOR not in found
    assert CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR not in found
    assert CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2 not in found
    assert len(found) == 3


def test_a_disabled_entity_is_not_proposed():
    found = _detect(_hass(), FULL, disabled=("current_sell_rate",))
    assert CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR not in found


def test_two_lv_v2_accounts_are_ambiguous_and_propose_nothing():
    hass = _hass((("loaded", ENTRY), ("loaded", "OTHER")))
    assert _detect(hass) == {}


def test_a_not_loaded_entry_proposes_nothing():
    assert _detect(_hass((("setup_error", ENTRY),))) == {}


def test_prefill_fills_only_empty_fields():
    saved = {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.my_own_import",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: None,
        CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: "",
    }
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        out = pa.with_detected_profile(_hass(), saved)
    assert out[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.my_own_import"  # kept
    assert out[CONF_SOLVER_EXPORT_PRICE_SENSOR] == FULL["sell_flex_up"]
    assert out[CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR] == FULL["flex_up_forecast"]
    assert (
        out[CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR] == FULL["current_sell_rate"]
    )
    assert saved[CONF_SOLVER_EXPORT_PRICE_SENSOR] is None  # input not mutated


def _notifications(hass):
    return {
        c.args[2]["notification_id"]: c.args[2]["message"]
        for c in hass.services.async_call.call_args_list
        if c.args[1] == "create"
    }


def test_startup_notifies_when_lv_v2_is_installed_and_fields_are_empty():
    hass = _hass()
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, {}))
    notes = _notifications(hass)
    assert pa.NOTIFY_DETECTED_ID in notes
    assert "6 of Nimbus's 6" in notes[pa.NOTIFY_DETECTED_ID]


def test_a_five_field_profile_is_told_about_the_second_p2p_source():
    """nimbus #1537: an install that saved the original five fields is told,
    once at startup and without any change, that the second matched-rate
    source (Sell P2P Matched Cost) is available."""
    hass = _hass()
    five = {
        f: FULL[k]
        for f, k in pa.LV_V2_PROFILE.items()
        if f != CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2
    }
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, five))
    notes = _notifications(hass)
    assert "1 of Nimbus's 6" in notes[pa.NOTIFY_DETECTED_ID]


def test_the_second_p2p_source_is_prefilled_when_empty():
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        out = pa.with_detected_profile(_hass(), {})
    assert (
        out[CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR_2] == FULL["sell_matched_cost"]
    )


def test_startup_is_quiet_once_the_profile_is_set():
    hass = _hass()
    complete = {f: FULL[k] for f, k in pa.LV_V2_PROFILE.items()}
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, complete))
    assert _notifications(hass) == {}


def test_matched_rate_without_a_price_array_is_flagged():
    """The tester's exact state: matched rate set, array not."""
    hass = _hass()
    opts = {CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR: FULL["current_sell_rate"]}
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, opts))
    notes = _notifications(hass)
    assert pa.NOTIFY_P2P_TRAP_ID in notes
    assert FULL["flex_up_forecast"] in notes[pa.NOTIFY_P2P_TRAP_ID]


def test_without_lv_v2_only_the_trap_check_runs():
    hass = _hass(())
    with patch.object(pa.er, "async_get", return_value=_Registry({})):
        asyncio.run(pa.async_notify_pricing_setup(hass, {}))
    assert _notifications(hass) == {}


def test_a_field_pointing_at_a_missing_entity_is_flagged_and_offered_lv_v2():
    """The tester's suspected state: settings copied from the reference
    household, naming its own writer sensors, which his install lacks."""
    hass = _hass()
    ghost = "sensor.localvolts_price_forecast"
    hass.states.get = MagicMock(
        side_effect=lambda eid: None if eid == ghost else object()
    )
    opts = {CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: ghost}
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        assert pa.missing_profile_entities(hass, opts) == {
            CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: ghost
        }
        out = pa.with_detected_profile(hass, opts)
        asyncio.run(pa.async_notify_pricing_setup(hass, opts))
    assert out[CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR] == FULL["flex_up_forecast"]
    notes = _notifications(hass)
    assert ghost in notes[pa.NOTIFY_MISSING_ID]


def test_a_real_entity_of_another_integration_is_never_replaced():
    hass = _hass()
    opts = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.amber_general_price"}
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        out = pa.with_detected_profile(hass, opts)
    assert out[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.amber_general_price"


# --- nimbus #1564: fees on top of Buy Flex Up are counted twice ---------------

REFERENCE_FEES = {
    "solver_network_fee_default_rate": 0.066759,
    "solver_network_fee_1_rate": 0.214863,
    "solver_network_fee_2_rate": 0.004774,
    "solver_network_fee_3_rate": 0.0,
    "solver_flat_fee_rate": 0.008246,
}


def _complete():
    return {f: FULL[k] for f, k in pa.LV_V2_PROFILE.items()}


def test_fees_set_alongside_buy_flex_up_are_flagged():
    """The reference household before 2026-10-06: Flex Up as import, plus its
    own network and flat fees, which Flex Up already contains."""
    hass = _hass()
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(
            pa.async_notify_pricing_setup(hass, {**_complete(), **REFERENCE_FEES})
        )
    # nimbus #1574 stage 2: reported as a Repair (setup_health), not a
    # notification; the startup check only dismisses the old notification.
    assert _notifications(hass) == {}
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        doubled = pa.detect_fees_doubled(hass, {**_complete(), **REFERENCE_FEES})
    assert doubled is not None
    flex_up, listed = doubled
    assert flex_up == "sensor.localvolts_v2_buy_flex_up"
    assert (
        "solver_flat_fee_rate" in listed and "solver_network_fee_3_rate" not in listed
    )


def test_zero_fees_with_buy_flex_up_are_quiet():
    hass = _hass()
    zero = dict.fromkeys(REFERENCE_FEES, 0.0)
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, {**_complete(), **zero}))
    assert _notifications(hass) == {}


def test_fees_with_a_different_import_sensor_are_not_flagged():
    """A generic retail price sensor may genuinely exclude network charges;
    only LocalVolts' Flex Up is known to include them."""
    hass = _hass()
    opts = {
        **_complete(),
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.my_spot_price",
        **REFERENCE_FEES,
    }
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, opts))
    assert pa.NOTIFY_FEES_DOUBLED_ID not in _notifications(hass)


def test_fees_without_lv_v2_are_not_flagged():
    hass = _hass(entries=())
    with patch.object(pa.er, "async_get", return_value=_Registry({})):
        asyncio.run(pa.async_notify_pricing_setup(hass, dict(REFERENCE_FEES)))
    assert pa.NOTIFY_FEES_DOUBLED_ID not in _notifications(hass)


def test_unparseable_fee_values_count_as_zero():
    hass = _hass()
    opts = {
        **_complete(),
        "solver_flat_fee_rate": "n/a",
        "solver_network_fee_1_rate": None,
    }
    with patch.object(pa.er, "async_get", return_value=_Registry(FULL)):
        asyncio.run(pa.async_notify_pricing_setup(hass, opts))
    assert _notifications(hass) == {}
