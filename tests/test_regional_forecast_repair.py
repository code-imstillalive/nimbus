"""16 Oct 2026, a tester's install: LocalVolts v2 prices, no regional spot
forecast. Prices past LocalVolts' own forecast were one flat level and the
stale-source fallback (#1793) had no AEMO series to switch to, and nothing
said so. An Australian-NEM install with no regional forecast now gets a
Repair that names the forecast to choose, or what to install, and clears
once the field is set."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import pricing_autodetect as pa
from custom_components.nimbus_load import setup_health as sh

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

LV = {
    "solver_import_price_sensor": "sensor.localvolts_v2_buy_flex_up",
    "solver_export_price_sensor": "sensor.localvolts_v2_sell_flex_up",
}


def _check(options, *, platform="localvolts_v2", found=None):
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform=platform)
    spot = (
        ({"solver_regional_spot_forecast_sensor": found}, "NEM PD7DAY")
        if found
        else ({}, None)
    )
    with (
        patch.object(pa.er, "async_get", return_value=registry),
        patch.object(pa, "detect_regional_spot_forecast", return_value=spot),
    ):
        return pa.detect_missing_regional_forecast(MagicMock(), options)


def test_a_nem_install_without_one_is_told_what_to_install():
    hit = _check(LV)
    assert hit is not None
    found, where = hit
    assert "No regional forecast integration" in found
    assert "purcell-lab/nem_pd7day" in where


def test_an_installed_forecast_is_named():
    found, where = _check(LV, found="sensor.nem_pd7day_qld1_nem_spot_price_forecast")
    assert "`sensor.nem_pd7day_qld1_nem_spot_price_forecast`" in found
    assert "Review pricing suggestions" in where


def test_a_template_sensor_named_after_the_retailer_counts():
    """The reference household's own LocalVolts template sensors."""
    options = {"solver_import_price_sensor": "sensor.localvolts_costs_flex_up"}
    assert _check(options, platform="template") is not None


def test_no_repair_once_set_or_off_the_nem():
    assert _check({**LV, "solver_regional_spot_forecast_sensor": "sensor.x"}) is None
    other = {"solver_import_price_sensor": "sensor.octopus_agile_import"}
    assert _check(other, platform="octopus_energy") is None
    assert _check({}) is None


def test_it_is_a_repair_with_its_own_text():
    issues = sh.evaluate_health(
        options={},
        forecasts=[],
        hub_up_for=timedelta(0),
        regional_forecast_missing=(
            "No regional forecast integration is installed.",
            "Install it.",
        ),
    )
    kinds = [i.kind for i in issues]
    assert sh.KIND_NO_REGIONAL_FORECAST in kinds
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((ROOT / name).read_text(encoding="utf-8"))
        entry = text["issues"][sh.KIND_NO_REGIONAL_FORECAST]
        assert "{found}" in entry["description"] and "{where}" in entry["description"]
