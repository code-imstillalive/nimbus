"""Pricing detection: which installed price integrations Nimbus recognises,
what it proposes (never applies without confirmation), and what it tells a
household whose pricing cannot work as set (#1837).

A fake entity registry holds each provider's own unique_id shapes. Covers
what lived in #1067, #1550, #1686, LocalVolts v2, Amber (Express and core),
NEM PD7DAY, AEMO NEM Data and OpenADR detection files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

pa = importlib.import_module("custom_components.nimbus_load.pricing_autodetect")
const = importlib.import_module("custom_components.nimbus_load.const")
pi = importlib.import_module("custom_components.nimbus_load.price_intervals")

IMPORT, EXPORT = (
    const.CONF_SOLVER_IMPORT_PRICE_SENSOR,
    const.CONF_SOLVER_EXPORT_PRICE_SENSOR,
)
ARRAY = const.CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR
SPOT = const.CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR


class World:
    """Installed integrations: config entries and their registry entities."""

    def __init__(self):
        self.entries: dict[str, list] = {}
        self.regs: list = []
        self.states: dict = {}
        self.calls: list = []

    def add(
        self, domain, entity_id, unique_id, *, entry="e1", loaded=True, disabled=False
    ):
        entries = self.entries.setdefault(domain, [])
        if not any(e.entry_id == f"{domain}-{entry}" for e in entries):
            entries.append(
                SimpleNamespace(
                    entry_id=f"{domain}-{entry}",
                    state=SimpleNamespace(value="loaded" if loaded else "setup_error"),
                )
            )
        self.regs.append(
            SimpleNamespace(
                entity_id=entity_id,
                unique_id=unique_id,
                platform=domain,
                domain=entity_id.split(".")[0],
                config_entry_id=f"{domain}-{entry}",
                disabled_by="user" if disabled else None,
            )
        )
        self.states[entity_id] = SimpleNamespace(state="0.1", attributes={})
        return entity_id

    def hass(self):
        async def call(domain, service, data, *a, **kw):
            self.calls.append(
                (service, data["notification_id"], data.get("message", ""))
            )

        return SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda domain: list(self.entries.get(domain, []))
            ),
            states=SimpleNamespace(
                get=self.states.get, async_all=lambda domain=None: []
            ),
            services=SimpleNamespace(async_call=call),
        )


@pytest.fixture
def world(monkeypatch):
    w = World()
    er = sys.modules["homeassistant.helpers.entity_registry"]

    def registry(hass):
        by_id = {r.entity_id: r for r in w.regs}
        return SimpleNamespace(
            async_get=by_id.get,
            async_get_entity_id=lambda domain, platform, uid: next(
                (
                    r.entity_id
                    for r in w.regs
                    if r.platform == platform
                    and r.unique_id == uid
                    and r.domain == domain
                ),
                None,
            ),
        )

    monkeypatch.setattr(er, "async_get", registry, raising=False)
    monkeypatch.setattr(
        er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: [r for r in w.regs if r.config_entry_id == entry_id],
        raising=False,
    )
    monkeypatch.setattr(
        pa.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        lambda states: (None, None),
    )
    return w


def _localvolts(w, *, entry="e1"):
    lv = f"{pa.LV_V2_DOMAIN}-{entry}"
    return {
        field: w.add(pa.LV_V2_DOMAIN, f"sensor.lv_{key}", f"{lv}_{key}", entry=entry)
        for field, key in pa.LV_V2_PROFILE.items()
    }


# --- recognising each provider ------------------------------------------------------------


def test_localvolts_v2_is_recognised_from_its_registry_entries(world, monkeypatch):
    # test_localvolts_v2_is_recognised_from_its_registry_entries
    profile = _localvolts(world)
    world.add(
        pa.LV_V2_DOMAIN,
        "sensor.lv_off",
        f"{pa.LV_V2_DOMAIN}-e1_{next(iter(pa.LV_V2_OPTIONAL_PROFILE.values()))}",
        disabled=True,
    )
    assert pa.detect_localvolts_v2_profile(world.hass()) == profile

    # test_two_localvolts_sites_or_none_loaded_is_no_proposal
    _localvolts(world, entry="a")
    _localvolts(world, entry="b")
    assert pa.detect_localvolts_v2_profile(world.hass()) == {}
    other = World()
    other.add(pa.LV_V2_DOMAIN, "sensor.x", "x", loaded=False)
    assert pa.detect_localvolts_v2_profile(other.hass()) == {}

    # test_each_retail_provider_is_proposed_only_for_a_single_site
    for domain, uids, expected in [
        (
            pi.AMBER_EXPRESS_DOMAIN,
            {
                "site1_general_price": "sensor.ae_buy",
                "site1_feed_in_price": "sensor.ae_sell",
            },
            {IMPORT: "sensor.ae_buy", EXPORT: "sensor.ae_sell"},
        ),
        (
            pi.AMBER_EXPRESS_DOMAIN,
            {
                "site1_general_price": "sensor.ae_buy",
                "site2_general_price": "sensor.ae2",
            },
            {},
        ),
        (
            pi.AMBER_CORE_DOMAIN,
            {
                "01J-forecasts-general": "sensor.amber_buy",
                "01J-forecasts-feed_in": "sensor.amber_sell",
            },
            {IMPORT: "sensor.amber_buy", EXPORT: "sensor.amber_sell"},
        ),
        (
            pi.AMBER_CORE_DOMAIN,
            {
                "01J-forecasts-general": "sensor.amber_buy",
                "02K-forecasts-general": "sensor.b",
            },
            {},
        ),
        (
            pi.PD7DAY_DOMAIN,
            {"qld1_tariff": "sensor.pd7_buy", "qld1_export_tariff": "sensor.pd7_sell"},
            {IMPORT: "sensor.pd7_buy", EXPORT: "sensor.pd7_sell"},
        ),
        (pi.PD7DAY_DOMAIN, {"qld1_tariff": "sensor.a", "nsw1_tariff": "sensor.b"}, {}),
    ]:
        for uid, eid in uids.items():
            world.add(domain, eid, uid)
        detect = {
            pi.AMBER_EXPRESS_DOMAIN: pa.detect_amber_express_profile,
            pi.AMBER_CORE_DOMAIN: pa.detect_amber_core_profile,
            pi.PD7DAY_DOMAIN: pa.detect_pd7day_tariff_pair,
        }[domain]
        assert detect(world.hass()) == expected

    # test_a_regional_forecast_prefers_pd7day_and_picks_the_home_region_among_several
    with monkeypatch.context() as mp:
        world.add(
            pi.AEMO_NEM_DOMAIN, "sensor.aemo_qld", f"x_qld1_{pi.AEMO_NEM_FORECAST_KEY}"
        )
        assert pa.detect_regional_spot_forecast(world.hass()) == (
            {SPOT: "sensor.aemo_qld"},
            "AEMO NEM Data",
        )
        world.add(pi.PD7DAY_DOMAIN, "sensor.pd7_qld", "nem_pd7day_qld1_forecast")
        world.add(pi.PD7DAY_DOMAIN, "sensor.pd7_nsw", "nem_pd7day_nsw1_forecast")
        # Two PD7DAY regions and no home region: not PD7DAY, so AEMO.
        assert pa.detect_regional_spot_forecast(world.hass()) == (
            {SPOT: "sensor.aemo_qld"},
            "AEMO NEM Data",
        )
        assert pa.detect_pd7day_forecast(world.hass()) == {}
        mp.setattr(
            pa.sensor_discovery,
            "resolve_geocoded_region_and_prefix",
            lambda states: ("NSW1", "200"),
        )
        assert pa.detect_regional_spot_forecast(world.hass()) == (
            {SPOT: "sensor.pd7_nsw"},
            "NEM PD7DAY",
        )

    # test_suggestions_fill_only_empty_fields_from_the_dashboard_then_the_providers
    lv = _localvolts(world)
    suggestions = pa.pricing_suggestions(
        world.hass(),
        {EXPORT: "sensor.mine"},
        {IMPORT: "sensor.dash_buy", EXPORT: "sensor.dash_sell"},
    )
    by_field = {s["field"]: s for s in suggestions}
    assert (
        by_field[IMPORT]["proposed"] == "sensor.dash_buy"
        and by_field[IMPORT]["source"] == "Energy Dashboard"
    )
    assert EXPORT not in by_field  # never over a saved choice
    assert by_field[ARRAY] == {
        "field": ARRAY,
        "label": pa.PRICING_FIELD_LABELS.get(ARRAY, ARRAY),
        "current": None,
        "proposed": lv[ARRAY],
        "source": "LocalVolts v2",
    }
    merged, applied = pa.apply_pricing_suggestions({EXPORT: "sensor.mine"}, suggestions)
    assert merged[EXPORT] == "sensor.mine" and merged[IMPORT] == "sensor.dash_buy"
    assert [a["field"] for a in applied] == [s["field"] for s in suggestions]

    # test_a_second_provider_becomes_the_second_price_source
    world.add(pi.AMBER_CORE_DOMAIN, "sensor.amber_buy", "01J-forecasts-general")
    world.add(pi.AMBER_CORE_DOMAIN, "sensor.amber_sell", "01J-forecasts-feed_in")
    world.add(pi.PD7DAY_DOMAIN, "sensor.pd7_buy", "qld1_tariff")
    proposed = {(f, e) for f, e, _s in pa._proposals(world.hass(), {})}
    assert (IMPORT, "sensor.amber_buy") in proposed and (
        EXPORT,
        "sensor.amber_sell",
    ) in proposed
    assert (const.CONF_SOLVER_IMPORT_PRICE_SENSOR_2, "sensor.pd7_buy") in proposed
    full = {
        const.CONF_SOLVER_IMPORT_PRICE_SENSOR_2: "x",
        const.CONF_SOLVER_IMPORT_PRICE_SENSOR_3: "y",
    }
    assert pa._extra_source_proposals(full, [("PD7", {IMPORT: "sensor.z"})]) == []
    assert (
        pa._extra_source_proposals(
            {IMPORT: "sensor.z"}, [("PD7", {IMPORT: "sensor.z"})]
        )
        == []
    )  # already in use
    assert pa.with_detected_profile(world.hass(), {})[IMPORT] == "sensor.amber_buy"

    # test_with_a_price_array_set_a_regional_forecast_is_proposed_too
    world.add(
        pi.AEMO_NEM_DOMAIN, "sensor.aemo_qld", f"x_qld1_{pi.AEMO_NEM_FORECAST_KEY}"
    )
    assert pa._proposals(world.hass(), {}) == []
    assert pa._proposals(world.hass(), {ARRAY: "sensor.array"}) == [
        (SPOT, "sensor.aemo_qld", "AEMO NEM Data")
    ]

    # test_fees_on_top_of_a_price_that_already_includes_them_are_flagged
    lv = _localvolts(world)
    fee = pa._FEE_RATE_KEYS[0]
    assert pa.detect_fees_doubled(world.hass(), {IMPORT: lv[IMPORT], fee: 0.05}) == (
        lv[IMPORT],
        f"`{fee}` = 0.05",
    )
    assert pa.detect_fees_doubled(world.hass(), {IMPORT: lv[IMPORT], fee: "x"}) is None
    assert (
        pa.detect_fees_doubled(world.hass(), {IMPORT: "sensor.other", fee: 0.05})
        is None
    )
    tariff = world.add(pi.PD7DAY_DOMAIN, "sensor.pd7_buy", "qld1_tariff")
    world.add(pi.PD7DAY_DOMAIN, "sensor.pd7_sell", "qld1_export_tariff")
    assert pa.detect_fees_on_pd7day_tariff(
        world.hass(), {IMPORT: tariff, fee: 0.1}
    ) == (tariff, f"`{fee}` = 0.1")
    assert (
        pa.detect_fees_on_pd7day_tariff(
            world.hass(), {IMPORT: "sensor.pd7_sell", fee: 0.1}
        )
        is None
    )
    assert pa.detect_fees_on_pd7day_tariff(world.hass(), {IMPORT: tariff}) is None
    assert pa.detect_fees_on_pd7day_tariff(world.hass(), {}) is None


# --- proposals: suggested, never applied without a yes ------------------------------------


# --- what a household is told --------------------------------------------------------------


def test_a_nem_price_without_a_regional_forecast_is_pointed_at_one(
    world, caplog, monkeypatch
):
    # test_a_nem_price_without_a_regional_forecast_is_pointed_at_one
    assert (
        pa.detect_missing_regional_forecast(
            world.hass(), {IMPORT: "sensor.house_price"}
        )
        is None
    )
    assert (
        pa.detect_missing_regional_forecast(
            world.hass(), {IMPORT: "sensor.amber_general", SPOT: "sensor.s"}
        )
        is None
    )
    nothing = pa.detect_missing_regional_forecast(
        world.hass(), {IMPORT: "sensor.amber_general"}
    )
    assert (
        nothing[0] == "No regional forecast integration is installed."
        and "nem_pd7day" in nothing[1]
    )
    world.add(
        pi.AEMO_NEM_DOMAIN, "sensor.aemo_qld", f"x_qld1_{pi.AEMO_NEM_FORECAST_KEY}"
    )
    found = pa.detect_missing_regional_forecast(
        world.hass(), {IMPORT: "sensor.amber_general"}
    )
    assert found[0] == "Nimbus found `sensor.aemo_qld` (AEMO NEM Data)."

    # test_once_its_fields_are_chosen_a_provider_is_not_reported_again
    profile = _localvolts(world)
    _first, *rest = profile
    partly = _notices(world, {f: profile[f] for f in rest})
    assert f"1 of Nimbus's {len(pa.LV_V2_PROFILE)}" in partly[pa.NOTIFY_DETECTED_ID]
    world.calls.clear()
    assert pa.NOTIFY_DETECTED_ID not in _notices(world, profile)

    # test_each_detected_provider_has_its_own_notice
    for domain, uids, notice in [
        (
            pi.AMBER_EXPRESS_DOMAIN,
            {"s_general_price": "sensor.ae_buy"},
            pa.NOTIFY_AMBER_EXPRESS_DETECTED_ID,
        ),
        (
            pi.AMBER_CORE_DOMAIN,
            {"01J-forecasts-general": "sensor.amber_buy"},
            pa.NOTIFY_AMBER_CORE_DETECTED_ID,
        ),
        (pi.OPENADR_DOMAIN, {"ven_price": "sensor.adr"}, pa.NOTIFY_OPENADR_DETECTED_ID),
    ]:
        for uid, eid in uids.items():
            world.add(domain, eid, uid)
        notices = _notices(world, {})
        assert notice in notices
        assert (
            "Nothing has been changed" in notices[notice]
            or "nothing changes unless you confirm" in notices[notice]
        )

    # test_a_matched_rate_without_a_price_array_is_called_out
    notices = _notices(
        world, {const.CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR: "sensor.rate"}
    )
    assert "P2P" in notices[pa.NOTIFY_P2P_TRAP_ID]
    world.add(
        pi.AEMO_NEM_DOMAIN, "sensor.aemo_qld", f"x_qld1_{pi.AEMO_NEM_FORECAST_KEY}"
    )
    notices = _notices(world, {ARRAY: "sensor.array"})
    assert "`sensor.aemo_qld`" in notices[pa.NOTIFY_AEMO_DETECTED_ID]
    assert ("dismiss", pa.NOTIFY_FEES_DOUBLED_ID, "") in world.calls

    # test_a_failing_notification_never_breaks_setup
    hass = world.hass()

    async def broken(*a, **kw):
        raise RuntimeError("no notifications")

    hass.services.async_call = broken
    asyncio.run(pa.async_notify_pricing_setup(hass, {IMPORT: "sensor.gone"}))

    # test_a_matched_rate_without_an_array_names_localvolts_own_array
    profile = _localvolts(world)
    notices = _notices(
        world, {const.CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR: "sensor.rate"}
    )
    array = profile[const.CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR]
    assert f"`{array}`" in notices[pa.NOTIFY_P2P_TRAP_ID]

    # test_fees_on_top_of_flex_up_are_warned_about_in_the_log
    caplog.clear()
    with monkeypatch.context() as mp:
        mp.setattr(pa, "detect_fees_doubled", lambda *a, **k: ("sensor.lv", "0.05"))
        import logging

        with caplog.at_level(logging.WARNING):
            _notices(world, {})
        assert "counted twice" in caplog.text and "0.05" in caplog.text

    # test_a_second_provider_brings_its_export_price_too
    filled = {}
    out = pa._extra_source_proposals(
        filled,
        [("Amber", {IMPORT: "sensor.amber_buy", EXPORT: "sensor.amber_sell"})],
    )
    assert [(f, e) for f, e, _s in out] == [
        (const.CONF_SOLVER_IMPORT_PRICE_SENSOR_2, "sensor.amber_buy"),
        (const.CONF_SOLVER_EXPORT_PRICE_SENSOR_2, "sensor.amber_sell"),
    ]


def _notices(world, options):
    asyncio.run(pa.async_notify_pricing_setup(world.hass(), options))
    return {nid: msg for service, nid, msg in world.calls if service == "create"}


def test_a_price_sensor_named_for_a_nem_retailer_counts_as_nem(world):
    world.add(pa.LV_V2_DOMAIN, "sensor.my_buy", "lv-e1_buy_flex_up")
    hass = world.hass()
    assert pa._is_nem_price(hass, "sensor.my_buy")  # by its integration
    assert pa._is_nem_price(hass, "sensor.localvolts_costs_flex_up")  # by its name
    assert not pa._is_nem_price(hass, "sensor.octopus_agile_import")
    assert not pa._is_nem_price(hass, None)
