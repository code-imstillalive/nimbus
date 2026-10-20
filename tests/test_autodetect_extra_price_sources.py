"""Household, 20 Oct 2026: price sources 3 and 4 found, not typed.

The Grid Prices step numbers the price sources 1 (the provider), 2 (AEMO),
3 and 4 (other forecasts to compare). Auto-detect already proposed a
provider; a site with LocalVolts as its provider and Amber and NEM PD7DAY
installed got neither of the other two proposed. Now a retail profile that is
not the provider's, and a single PD7DAY network tariff, are proposed for
sources 3 and 4 -- empty fields only, under Review pricing suggestions, like
every proposal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import pricing_autodetect as pa
from custom_components.nimbus_load.const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
)

LV = {
    CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.lv_buy",
    CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.lv_sell",
}
AMBER = {
    CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.amber_general",
    CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.amber_feed_in",
}
PD7 = {
    CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.pd7_tariff",
    CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.pd7_export_tariff",
}


def _proposals(options, lv=None, amber=None, core=None, pd7=None):
    with (
        patch.object(pa, "detect_localvolts_v2_profile", return_value=lv or {}),
        patch.object(pa, "detect_amber_express_profile", return_value=amber or {}),
        patch.object(pa, "detect_amber_core_profile", return_value=core or {}),
        patch.object(pa, "detect_pd7day_tariff_pair", return_value=pd7 or {}),
    ):
        return {f: (e, s) for f, e, s in pa._proposals(MagicMock(), options)}


def test_amber_is_source_3_and_pd7day_source_4_beside_a_localvolts_provider():
    got = _proposals({}, lv=LV, amber=AMBER, pd7=PD7)
    assert got[CONF_SOLVER_IMPORT_PRICE_SENSOR] == ("sensor.lv_buy", "LocalVolts v2")
    assert got[CONF_SOLVER_IMPORT_PRICE_SENSOR_2] == (
        "sensor.amber_general",
        "Amber Express",
    )
    assert got[CONF_SOLVER_EXPORT_PRICE_SENSOR_2][0] == "sensor.amber_feed_in"
    assert got[CONF_SOLVER_IMPORT_PRICE_SENSOR_3] == (
        "sensor.pd7_tariff",
        "NEM PD7DAY tariff",
    )
    assert got[CONF_SOLVER_EXPORT_PRICE_SENSOR_3][0] == "sensor.pd7_export_tariff"


def test_the_provider_is_never_proposed_again_as_an_extra_source():
    # Amber is the provider (no LocalVolts): it fills source 1, not 3.
    got = _proposals({}, amber=AMBER, pd7=PD7)
    assert got[CONF_SOLVER_IMPORT_PRICE_SENSOR][0] == "sensor.amber_general"
    assert got[CONF_SOLVER_IMPORT_PRICE_SENSOR_2][0] == "sensor.pd7_tariff"
    assert CONF_SOLVER_IMPORT_PRICE_SENSOR_3 not in got
    # A sensor the household already set anywhere is not proposed elsewhere.
    got = _proposals(
        {**LV, CONF_SOLVER_IMPORT_PRICE_SENSOR_3: "sensor.amber_general"},
        amber=AMBER,
        pd7=PD7,
    )
    assert got == {
        CONF_SOLVER_IMPORT_PRICE_SENSOR_2: ("sensor.pd7_tariff", "NEM PD7DAY tariff"),
        CONF_SOLVER_EXPORT_PRICE_SENSOR_2: (
            "sensor.pd7_export_tariff",
            "NEM PD7DAY tariff",
        ),
    }


def test_a_set_extra_source_is_never_replaced():
    options = {
        **LV,
        CONF_SOLVER_IMPORT_PRICE_SENSOR_2: "sensor.mine",
        CONF_SOLVER_IMPORT_PRICE_SENSOR_3: "sensor.also_mine",
    }
    assert _proposals(options, amber=AMBER, pd7=PD7) == {}


def _hass_with_pd7(regs):
    hass = MagicMock()
    entry = SimpleNamespace(state=SimpleNamespace(value="loaded"), entry_id="E1")
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda d: [entry] if d == "nem_pd7day" else []
    )
    with (
        patch.object(pa.er, "async_get", return_value=MagicMock()),
        patch.object(pa.er, "async_entries_for_config_entry", return_value=regs),
    ):
        return pa.detect_pd7day_tariff_pair(hass)


def _reg(uid, entity_id, disabled=None):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id, disabled_by=disabled, domain="sensor"
    )


def test_one_pd7day_tariff_is_paired_with_its_export_tariff():
    regs = [
        _reg("nem_pd7day_qld1_forecast", "sensor.spot"),
        _reg("E1_QLD1_energex_6900_tariff", "sensor.t6900"),
        _reg("E1_QLD1_energex_6900_export_tariff", "sensor.t6900x"),
    ]
    assert _hass_with_pd7(regs) == {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.t6900",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.t6900x",
    }


def test_several_pd7day_tariffs_propose_nothing():
    """Which tariff the meter is on is the household's to say."""
    regs = [
        _reg("E1_QLD1_energex_6900_tariff", "sensor.t6900"),
        _reg("E1_QLD1_energex_8900_tariff", "sensor.t8900"),
        _reg("E1_QLD1_energex_6900_export_tariff", "sensor.t6900x"),
    ]
    assert _hass_with_pd7(regs) == {}
