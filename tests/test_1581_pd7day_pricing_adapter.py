"""nimbus #1581: NEM PD7DAY's forecasts through the real pricing path.

PD7DAY publishes three price bases in one row shape (`time` = interval start,
`nemtime` = interval end): wholesale (`raw_value`, `calibrated`, `value`), an
import tariff (`value` = calibrated spot + that tariff's network component)
and an export tariff. Nimbus reads the published `value` as is, keeps a null
value missing, measures coverage per interval, prefers PD7DAY over AEMO NEM
Data as the regional spot forecast, and flags network fees counted twice when
the import price is a PD7DAY tariff.

Rows are Mark Purcell's live #1550 capture (6 Oct 2026), loaded from
`tests/fixtures/pricing/` and run through `solver_writer`, not a copy.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import price_intervals
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import pricing_autodetect as pa
from custom_components.nimbus_load import setup_health as sh
from custom_components.nimbus_load.const import (
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
PD7 = json.loads((FIXTURES / "1550_pd7day_qld_wholesale.json").read_text())
SAMPLES = {s["sample_alias"]: s["selected_native_rows"] for s in PD7["tariff_samples"]}
WHOLESALE = PD7["selected_native_rows"]
IMPORT = SAMPLES["pd7_import"]
EXPORT = SAMPLES["pd7_export"]
CONTINUATION = SAMPLES["pd7_continuation"]


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _generic(rows, grid):
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {"forecast": rows}}
    ):
        return solver_writer.resample_generic_price_forecast_with_coverage(
            "sensor.any_pd7day", grid
        )


# --- reading the published value --------------------------------------------


def test_tariff_value_is_read_as_published_not_rebuilt():
    """The import tariff's `value` already holds spot + network. It is read
    as is: not `spot`, not `spot_raw`, and nothing re-added."""
    values, real = _generic(IMPORT, [_t("2026-10-06T12:40:00+10:00")])
    assert values == [0.081069] and real == [True]


def test_export_tariff_value_is_read_as_published():
    values, _ = _generic(EXPORT, [_t("2026-10-06T12:40:00+10:00")])
    assert values == [0.024859]


def test_wholesale_reads_the_selected_calibrated_value_not_raw():
    values, _ = _generic(WHOLESALE, [_t("2026-10-06T12:40:00+10:00")])
    assert values == [0.043006]  # value == calibrated; raw_value is 0.02465


def test_regional_spot_forecast_path_is_unchanged():
    """Production reads PD7DAY here, through `calibrated`."""
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {"forecast": WHOLESALE}}
    ):
        pts = solver_writer.fetch_aemo_forecast("sensor.nem_pd7day_qld1_forecast")
    assert pts == [
        (_t("2026-10-06T12:30:00+10:00"), 0.043006),
        (_t("2026-10-14T03:30:00+10:00"), 0.081885),
    ]


def test_a_null_value_stays_missing_never_filled_from_raw_or_spot():
    row = dict(IMPORT[0], value=None)
    assert price_intervals.pd7day_intervals([row]) == []
    assert _generic([row], [_t("2026-10-06T12:40:00+10:00")]) is None


def test_negative_continuation_raw_is_not_used_when_calibrated_is_published():
    # raw_value -0.010995, calibrated/value 0.015555
    iv = price_intervals.pd7day_intervals(CONTINUATION)
    assert iv[0].value == 0.015555


def test_a_negative_published_value_is_kept():
    row = dict(EXPORT[0], value=-0.0123)
    assert price_intervals.pd7day_intervals([row])[0].value == -0.0123


# --- boundaries and coverage ------------------------------------------------


def test_interval_end_is_nemtime_and_the_last_interval_is_covered():
    grid = [
        _t("2026-10-14T03:45:00+10:00"),  # inside the final interval
        _t("2026-10-14T04:00:00+10:00"),  # its END: not covered
    ]
    _, real = _generic(WHOLESALE, grid)
    assert real == [True, False]


def test_the_hole_between_selected_rows_is_not_coverage():
    _, real = _generic(WHOLESALE, [_t("2026-10-08T12:00:00+10:00")])
    assert real == [False]


def test_continuation_does_not_cover_the_30_minute_seam():
    """#1550: Amber/Express ended 7 Oct 13:00; the continuation starts 13:30.
    Nimbus never treats 13:00-13:30 as covered by it."""
    cov = price_intervals.coverage(price_intervals.pd7day_intervals(CONTINUATION))
    assert cov.first_start == _t("2026-10-07T13:30:00+10:00")
    _, real = _generic(CONTINUATION, [_t("2026-10-07T13:10:00+10:00")])
    assert real == [False]


def test_basis_is_told_apart():
    assert price_intervals.pd7day_basis(WHOLESALE) == price_intervals.BASIS_WHOLESALE
    assert price_intervals.pd7day_basis(CONTINUATION) == price_intervals.BASIS_WHOLESALE
    assert price_intervals.pd7day_basis(IMPORT) == price_intervals.BASIS_TARIFF
    assert price_intervals.pd7day_basis(EXPORT) == price_intervals.BASIS_TARIFF


def test_plain_time_value_sensors_are_not_taken_for_pd7day():
    rows = [{"time": "2026-10-06T13:15:00+10:00", "value": 0.0848}]
    assert not price_intervals.is_pd7day_rows(rows)
    assert price_intervals.intervals_from_rows(rows) is None
    # Unchanged behaviour for {time, value}: real up to the last point.
    values, real = _generic(
        rows, [_t("2026-10-06T13:15:00+10:00"), _t("2026-10-06T13:20:00+10:00")]
    )
    assert values == [0.0848, 0.0848] and real == [True, False]


# --- proposal and precedence --------------------------------------------------

PD7_UID = "nem_pd7day_qld1_forecast"
AEMO_UID = "sensor.aemo_nem_qld1_current_30min_forecast"


def _reg(uid, entity_id, disabled=None):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id, disabled_by=disabled, domain="sensor"
    )


def _hass(by_domain):
    hass = MagicMock()
    entries = {
        d: [SimpleNamespace(state=SimpleNamespace(value="loaded"), entry_id=d)]
        for d in by_domain
    }
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda d: entries.get(d, [])
    )
    hass.states.async_all = MagicMock(return_value=[])
    hass.by_domain = by_domain
    return hass


def _spot(hass):
    with (
        patch.object(pa.er, "async_get", return_value=MagicMock()),
        patch.object(
            pa.er,
            "async_entries_for_config_entry",
            side_effect=lambda _r, entry_id: hass.by_domain[entry_id],
        ),
    ):
        return pa.detect_regional_spot_forecast(hass)


def test_pd7day_is_preferred_over_aemo():
    hass = _hass(
        {
            "nem_pd7day": [
                _reg(PD7_UID, "sensor.nem_pd7day_qld1_nem_spot_price_forecast")
            ],
            "aemo_nem": [_reg(AEMO_UID, AEMO_UID)],
        }
    )
    fields, source = _spot(hass)
    assert source == "NEM PD7DAY"
    assert fields == {
        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: (
            "sensor.nem_pd7day_qld1_nem_spot_price_forecast"
        )
    }


def test_aemo_when_pd7day_is_absent():
    fields, source = _spot(_hass({"aemo_nem": [_reg(AEMO_UID, AEMO_UID)]}))
    assert source == "AEMO NEM Data"
    assert fields == {CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: AEMO_UID}


def test_only_the_days_1_to_7_forecast_is_proposed():
    regs = [
        _reg("nem_pd7day_qld1_forecast_days27", "sensor.day_2_7"),
        _reg("E1_QLD1_energex_6900_tariff", "sensor.energex_tariff"),
        _reg("E1_QLD1_energex_6900_export_tariff", "sensor.energex_export"),
        _reg(PD7_UID, "sensor.renamed_pd7", disabled="user"),
    ]
    assert _spot(_hass({"nem_pd7day": regs})) == ({}, None)


def test_several_pd7day_regions_narrow_to_home():
    regs = [
        _reg(PD7_UID, "sensor.qld"),
        _reg("nem_pd7day_nsw1_forecast", "sensor.nsw"),
    ]
    with patch.object(
        pa.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        return_value=("NSW1", "200"),
    ):
        fields, _ = _spot(_hass({"nem_pd7day": regs}))
    assert fields == {CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: "sensor.nsw"}


# --- network fees counted twice on a PD7DAY tariff ------------------------------


def _fees_check(uid, platform="nem_pd7day", fees=None):
    registry = MagicMock()
    registry.async_get = MagicMock(
        return_value=SimpleNamespace(unique_id=uid, platform=platform)
    )
    options = {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.energex_tariff",
        **(fees or {}),
    }
    with patch.object(pa.er, "async_get", return_value=registry):
        return pa.detect_fees_on_pd7day_tariff(MagicMock(), options)


FEES = {CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE: 0.0434, CONF_SOLVER_FLAT_FEE_RATE: 0.02}


def test_fees_on_a_pd7day_import_tariff_are_flagged():
    hit = _fees_check("E1_QLD1_energex_6900_tariff", fees=FEES)
    assert hit is not None
    assert hit[0] == "sensor.energex_tariff"
    assert "0.0434" in hit[1] and "0.02" in hit[1]


def test_no_flag_without_fees_or_on_spot_or_export_or_another_integration():
    assert _fees_check("E1_QLD1_energex_6900_tariff") is None
    assert _fees_check(PD7_UID, fees=FEES) is None
    assert _fees_check("E1_QLD1_energex_6900_export_tariff", fees=FEES) is None
    assert _fees_check("x_tariff", platform="other", fees=FEES) is None


def test_it_is_a_repair_with_its_own_text():
    issues = sh.evaluate_health(
        options={},
        forecasts=[],
        hub_up_for=timedelta(0),
        fees_on_tariff=("sensor.energex_tariff", "`x` = 1"),
    )
    hit = [i for i in issues if i.kind == sh.KIND_FEES_ON_TARIFF]
    assert hit and hit[0].placeholders == {
        "tariff": "sensor.energex_tariff",
        "fees": "`x` = 1",
    }
    strings = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "custom_components/nimbus_load/strings.json"
        ).read_text(encoding="utf-8")
    )
    text = strings["issues"][sh.KIND_FEES_ON_TARIFF]["description"]
    assert "{tariff}" in text and "{fees}" in text
