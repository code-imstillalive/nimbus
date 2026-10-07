"""nimbus #1580: Amber Express prices through the real pricing path.

Amber Express's `forecast[].value` is already the household's selected
pricing basis, already sign-flipped for feed-in and already carries the
demand-window charge (hass-energy/amber-express `sensor.py`,
`AmberPriceSensor`). Nimbus reads it as published and takes only interval
BOUNDARIES from the case-sensitive `detailedForecast`, whose starts sit one
second past the boundary. Its general and feed-in sensors are proposed as the
import and export price where those are empty.

Rows are Mark Purcell's live #1550 capture (6 Oct 2026), extracted from the
published comment into `tests/fixtures/pricing/1550_amber_express.json` and
run through `solver_writer`, not a copy.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import price_intervals
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import pricing_autodetect as pa
from custom_components.nimbus_load.const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
)

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "pricing" / "1550_amber_express.json"
    ).read_text(encoding="utf-8")
)["samples"]
SIMPLE = FIXTURE["amber_express_general_simple"]["selected_native_rows"]
DETAIL = FIXTURE["amber_express_general_detail"]["selected_native_rows"]


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _read(simple, detail, grid, detailed_key="detailedForecast"):
    attrs = {"forecast": simple, "interpolation_mode": "previous"}
    if detail is not None:
        attrs[detailed_key] = detail
    with patch.object(solver_writer, "ha_get", return_value={"attributes": attrs}):
        return solver_writer.resample_generic_price_forecast_with_coverage(
            "sensor.amber_express_general_price", grid
        )


# --- the price is the published value ---------------------------------------


def test_value_is_the_published_selected_price():
    """13:20: simple 0.0613 is the selected advanced prediction (detail
    0.0612866) rounded upstream. Nimbus reads 0.0613, not a re-derived one."""
    values, real = _read(SIMPLE, DETAIL, [_t("2026-10-06T13:20:00+10:00")])
    assert values == [0.0613] and real == [True]


def test_feed_in_is_not_negated_again():
    """Amber Express negates feed-in in BOTH attributes. The simple value is
    already the earnings sign; Nimbus never flips it."""
    simple = [dict(r, value=abs(r["value"])) for r in SIMPLE]
    detail = copy.deepcopy(DETAIL)
    for d in detail:
        d["per_kwh"] = -d["per_kwh"]
    values, _ = _read(simple, detail, [_t("2026-10-06T13:20:00+10:00")])
    assert values == [0.0613]


def test_demand_window_is_not_added_again():
    """Upstream adds the demand-window charge to `value`; the detailed row
    only flags the window. Nimbus adds nothing."""
    simple = [dict(r) for r in SIMPLE]
    simple[1]["value"] = 0.0613 + 0.20  # as published inside a window
    detail = copy.deepcopy(DETAIL)
    detail[1]["demand_window"] = True
    values, _ = _read(simple, detail, [_t("2026-10-06T13:20:00+10:00")])
    assert values == [0.0613 + 0.20]


def test_zero_is_a_price_and_null_is_missing():
    simple = [dict(r) for r in SIMPLE]
    simple[1]["value"] = 0.0
    simple[2]["value"] = None
    iv = price_intervals.amber_express_intervals(simple, DETAIL)
    by_start = {i.start: i.value for i in iv}
    assert by_start[_t("2026-10-06T13:20:00+10:00")] == 0.0
    assert _t("2026-10-06T13:55:00+10:00") not in by_start


# --- boundaries -----------------------------------------------------------------


def test_one_second_starts_give_canonical_boundaries_and_mixed_widths():
    iv = {
        i.start: i.end for i in price_intervals.amber_express_intervals(SIMPLE, DETAIL)
    }
    assert iv[_t("2026-10-06T13:15:00+10:00")] == _t("2026-10-06T13:20:00+10:00")
    assert iv[_t("2026-10-06T13:55:00+10:00")] == _t("2026-10-06T14:00:00+10:00")
    assert iv[_t("2026-10-06T14:00:00+10:00")] == _t("2026-10-06T14:30:00+10:00")
    assert iv[_t("2026-10-07T12:30:00+10:00")] == _t("2026-10-07T13:00:00+10:00")


def test_final_end_is_the_detailed_end():
    _, real = _read(
        SIMPLE,
        DETAIL,
        [_t("2026-10-07T12:59:00+10:00"), _t("2026-10-07T13:00:00+10:00")],
    )
    assert real == [True, False]


def test_a_row_hours_before_the_next_is_not_hours_of_coverage():
    """14:30 has no detailed row in the sample and the next row is 22 h on."""
    _, real = _read(SIMPLE, DETAIL, [_t("2026-10-06T18:00:00+10:00")])
    assert real == [False]


def test_a_start_not_inside_the_first_minute_is_not_trusted():
    detail = copy.deepcopy(DETAIL)
    detail[0]["start_time"] = "2026-10-06T03:17:00+00:00"  # 2 min late
    iv = {
        i.start: i.end for i in price_intervals.amber_express_intervals(SIMPLE, detail)
    }
    # falls back to the next simple row, 5 minutes on
    assert iv[_t("2026-10-06T13:15:00+10:00")] == _t("2026-10-06T13:20:00+10:00")
    detail[0]["start_time"] = "2026-10-06T03:12:00+00:00"
    simple = [r for r in SIMPLE if r["time"] != "2026-10-06T13:20:00+10:00"]
    iv = {
        i.start: i.end for i in price_intervals.amber_express_intervals(simple, detail)
    }
    assert _t("2026-10-06T13:15:00+10:00") not in iv


def test_the_key_is_case_sensitive():
    """`detailed_forecast` is not Amber Express's key: plain {time, value}."""
    values, real = _read(
        SIMPLE,
        DETAIL,
        [_t("2026-10-07T12:45:00+10:00")],
        detailed_key="detailed_forecast",
    )
    assert values == [0.0345] and real == [False]  # past the last START


def test_without_detail_the_reader_is_unchanged():
    values, real = _read(SIMPLE, None, [_t("2026-10-06T13:20:00+10:00")])
    assert values == [0.0613] and real == [True]


# --- proposal -------------------------------------------------------------------

SITE = "01K2F0S9Y9AJJTF2115TMNR2BM"


def _reg(uid, entity_id, disabled=None):
    return SimpleNamespace(
        unique_id=uid, entity_id=entity_id, disabled_by=disabled, domain="sensor"
    )


def _hass(regs):
    hass = MagicMock()
    entry = SimpleNamespace(state=SimpleNamespace(value="loaded"), entry_id="AX")
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda d: [entry] if d == "amber_express" else []
    )
    hass.states.async_all = MagicMock(return_value=[])
    hass.states.get = MagicMock(side_effect=lambda eid: object())
    hass.services.async_call = AsyncMock()
    hass.regs = regs
    return hass


def _patches(hass, lv=None):
    return (
        patch.object(pa.er, "async_get", return_value=MagicMock()),
        patch.object(pa.er, "async_entries_for_config_entry", return_value=hass.regs),
        patch.object(pa, "detect_localvolts_v2_profile", return_value=lv or {}),
    )


def _run(hass, fn, lv=None):
    a, b, c = _patches(hass, lv)
    with a, b, c:
        return fn()


FULL = [
    _reg(f"{SITE}_general_price", "sensor.amber_express_general_price"),
    _reg(f"{SITE}_feed_in_price", "sensor.amber_express_feed_in_price"),
    _reg(f"{SITE}_forecast_horizon", "sensor.amber_express_forecast_horizon"),
]


def test_one_site_proposes_import_and_export():
    hass = _hass(FULL)
    assert _run(hass, lambda: pa.detect_amber_express_profile(hass)) == {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.amber_express_general_price",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.amber_express_feed_in_price",
    }


def test_two_sites_propose_nothing():
    hass = _hass([*FULL, _reg("OTHER_general_price", "sensor.other")])
    assert _run(hass, lambda: pa.detect_amber_express_profile(hass)) == {}


def test_a_disabled_sensor_is_left_out():
    regs = [FULL[0], _reg(f"{SITE}_feed_in_price", "sensor.x", disabled="user")]
    hass = _hass(regs)
    assert _run(hass, lambda: pa.detect_amber_express_profile(hass)) == {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.amber_express_general_price"
    }


def test_prefill_only_where_empty_and_after_localvolts():
    hass = _hass(FULL)
    set_already = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.mine"}
    out = _run(hass, lambda: pa.with_detected_profile(hass, dict(set_already)))
    assert out[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.mine"
    assert out[CONF_SOLVER_EXPORT_PRICE_SENSOR] == "sensor.amber_express_feed_in_price"
    lv = {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.localvolts_v2_buy_flex_up",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.localvolts_v2_sell_flex_up",
    }
    out = _run(hass, lambda: pa.with_detected_profile(hass, {}), lv=lv)
    assert out[CONF_SOLVER_IMPORT_PRICE_SENSOR] == "sensor.localvolts_v2_buy_flex_up"


def _notified(hass, options, lv=None):
    _run(hass, lambda: asyncio.run(pa.async_notify_pricing_setup(hass, options)), lv)
    return [
        c.args[2]["notification_id"]
        for c in hass.services.async_call.call_args_list
        if c.args[1] == "create"
    ]


def test_startup_notice_when_empty_and_not_with_localvolts():
    nid = pa.NOTIFY_AMBER_EXPRESS_DETECTED_ID
    assert nid in _notified(_hass(FULL), {})
    full = {
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.a",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.b",
    }
    assert nid not in _notified(_hass(FULL), full)
    lv = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.localvolts_v2_buy_flex_up"}
    assert nid not in _notified(_hass(FULL), {}, lv=lv)
