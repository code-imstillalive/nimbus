"""nimbus #1574 stage 1: offer to fill existing gaps from mappings the user
already confirmed (Mark's device contract on #1574), as one-click Repairs.

Mark's review of #1587 (6 Oct 2026) is the acceptance list pinned here:
- skipped-only plans are visible, write no options, and recover once the
  source is corrected; repeated refreshes do not duplicate;
- a power unit proves neither sign nor boundary: opposite-sign battery
  telemetry and partial solar are never promoted without the household's
  confirmation, sign settings are never changed, and a load forecast that is
  not a whole-house Power Signal is not offered as the cross-check;
- nothing is written until the household submits; the submit re-checks.

Replays two real installs (design docs/design/two-step-setup.md §11): the
reference household (everything configured: nothing offered) and the
tester's 6 Oct install (#1526). Units are not in diagnostics, so each test
states the units it assumes.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import setup_builder as sb

FIX = Path(__file__).resolve().parent / "fixtures" / "setup_builder"
PKG = Path(sb.__file__).resolve().parent
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

WHOLE_HOUSE = {"subentry_type": "power_signal", "signal_role": "other"}


def _options(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))["options"]


def _lookup(states):
    def look(entity_id):
        if entity_id not in states:
            return None
        unit, attrs = states[entity_id]
        return unit, {"unit_of_measurement": unit, **attrs}

    return look


REFERENCE_STATES = {
    "sensor.logger_battery_power": ("kW", {}),
    "sensor.combined_total_dc_power": ("W", {}),
    "sensor.cb_total_combined_power_adjusted_kw": ("kW", {}),
    "sensor.nimbus_cb_total_combined_power_adjusted_kw_forecast": (
        "kW",
        {"source_sensor": "sensor.cb_total_combined_power_adjusted_kw", **WHOLE_HOUSE},
    ),
}

# Units assumed for the test (W is what Smappee/GoodWe report). His load
# forecast is assumed to be a whole-house Power Signal.
TESTER_STATES = {
    "sensor.battery_power_1_2": ("W", {}),
    "sensor.solar_production_total": ("Wh", {}),
    "sensor.smappee_consumption_realtime": ("W", {}),
    "sensor.nimbus_smappee_consumption_realtime_forecast": (
        "kW",
        {"source_sensor": "sensor.smappee_consumption_realtime", **WHOLE_HOUSE},
    ),
}


# --- plan_setup_fills: pure ---------------------------------------------------


def test_the_reference_household_gets_nothing():
    plan = sb.plan_setup_fills(
        _options("reference_household.json"), _lookup(REFERENCE_STATES)
    )
    assert plan.empty, (plan.options, plan.skipped)


def test_the_testers_gaps_are_offered_and_his_wh_total_refused():
    plan = sb.plan_setup_fills(
        _options("tester_2026_10_06.json"), _lookup(TESTER_STATES)
    )
    assert plan.options == {
        "solver_battery_power_sensor": "sensor.battery_power_1_2",
        "solver_whole_house_cross_check_sensor": "sensor.smappee_consumption_realtime",
    }
    (skip,) = plan.skipped
    assert skip.target == "solver_solar_power_sensor"
    assert "'Wh', an energy total" in skip.reason


def test_after_he_picks_a_power_sensor_solar_is_offered_too():
    options = {
        **_options("tester_2026_10_06.json"),
        "solar_sensor": "sensor.solar_production",
    }
    plan = sb.plan_setup_fills(
        options, _lookup({**TESTER_STATES, "sensor.solar_production": ("W", {})})
    )
    assert plan.options["solver_solar_power_sensor"] == "sensor.solar_production"


def test_never_offers_a_set_field():
    options = {
        "battery_sensor": "sensor.batt",
        "solver_battery_power_sensor": "sensor.other",
    }
    assert sb.plan_setup_fills(options, _lookup({"sensor.batt": ("kW", {})})).empty


def test_a_field_the_user_cleared_is_not_offered_again():
    options = {
        "battery_sensor": "sensor.batt",
        "setup_builder_done": ["option:solver_battery_power_sensor"],
    }
    assert sb.plan_setup_fills(options, _lookup({"sensor.batt": ("kW", {})})).empty


def test_missing_or_unit_less_sensors_are_skipped_with_a_reason():
    options = {"battery_sensor": "sensor.gone", "solar_sensor": "sensor.nounit"}
    plan = sb.plan_setup_fills(options, _lookup({"sensor.nounit": (None, {})}))
    assert not plan.options
    why = {s.target: s for s in plan.skipped}
    assert why["solver_battery_power_sensor"].missing
    assert "no power unit" in why["solver_solar_power_sensor"].reason


def test_a_single_circuits_forecast_is_never_offered_as_the_cross_check():
    """Mark, #1587: a non-whole-house source_sensor stays unresolved."""
    states = {
        "sensor.nimbus_oven_forecast": (
            "kW",
            {"source_sensor": "sensor.oven", "subentry_type": "load"},
        ),
        "sensor.oven": ("kW", {}),
    }
    options = {"solver_load_forecast_sensor": "sensor.nimbus_oven_forecast"}
    plan = sb.plan_setup_fills(options, _lookup(states))
    assert plan.empty


def test_a_battery_or_solar_signal_is_never_offered_as_the_cross_check():
    states = {
        "sensor.nimbus_pv_forecast": (
            "kW",
            {
                "source_sensor": "sensor.pv",
                "subentry_type": "power_signal",
                "signal_role": "solar",
            },
        ),
        "sensor.pv": ("kW", {}),
    }
    options = {"solver_load_forecast_sensor": "sensor.nimbus_pv_forecast"}
    assert sb.plan_setup_fills(options, _lookup(states)).empty


# --- async_refresh_setup_fills: Repairs, never a write ------------------------


class _FakeIR:
    class IssueSeverity:
        WARNING = "warning"

    def __init__(self):
        self.issues = {}
        self.creates = 0

    def async_create_issue(self, hass, domain, issue_id, **kw):
        self.creates += 1
        self.issues[issue_id] = kw

    def async_delete_issue(self, hass, domain, issue_id):
        self.issues.pop(issue_id, None)


def _hass(states):
    hass = MagicMock()
    hass.data = {}

    def get(eid):
        if eid not in states:
            return None
        unit, attrs = states[eid]
        return SimpleNamespace(
            state="1.5", attributes={"unit_of_measurement": unit, **attrs}
        )

    hass.states.get.side_effect = get
    return hass


def _entry(options):
    return SimpleNamespace(entry_id="e1", options=options, subentries={})


def _refresh(hass, entry, fake, now=T0):
    from homeassistant import helpers

    with (
        patch.dict(sys.modules, {"homeassistant.helpers.issue_registry": fake}),
        patch.object(helpers, "issue_registry", fake, create=True),
    ):
        return asyncio.run(sb.async_refresh_setup_fills(hass, entry, now=now))


def _offer_id(target):
    return f"e1_setup_fill_{target}"


def _blocked_id(target):
    return f"e1_setup_fill_blocked_{target}"


def test_the_tester_sees_two_offers_and_one_reason_and_nothing_is_written():
    fake = _FakeIR()
    hass = _hass(TESTER_STATES)
    _refresh(hass, _entry(_options("tester_2026_10_06.json")), fake)
    assert set(fake.issues) == {
        _offer_id("solver_battery_power_sensor"),
        _offer_id("solver_whole_house_cross_check_sensor"),
        _blocked_id("solver_solar_power_sensor"),
    }
    offer = fake.issues[_offer_id("solver_battery_power_sensor")]
    assert offer["is_fixable"] is True
    assert offer["data"] == {
        "kind": "setup_fill",
        "entry_id": "e1",
        "target": "solver_battery_power_sensor",
        "sensor": "sensor.battery_power_1_2",
    }
    assert offer["translation_placeholders"]["reading"] == "1.5 W"
    assert fake.issues[_blocked_id("solver_solar_power_sensor")]["is_fixable"] is False
    hass.config_entries.async_update_entry.assert_not_called()


def test_the_reference_household_gets_no_repair_at_all():
    fake = _FakeIR()
    hass = _hass(REFERENCE_STATES)
    _refresh(hass, _entry(_options("reference_household.json")), fake)
    assert fake.issues == {}
    hass.config_entries.async_update_entry.assert_not_called()


def test_a_missing_only_plan_is_reported_after_a_grace_and_recovers():
    fake = _FakeIR()
    states = {}
    hass = _hass(states)
    entry = _entry({"battery_sensor": "sensor.late"})
    _refresh(hass, entry, fake, now=T0)
    assert fake.issues == {}  # may still be loading
    _refresh(hass, entry, fake, now=T0 + sb.MISSING_GRACE)
    blocked = fake.issues[_blocked_id("solver_battery_power_sensor")]
    assert "does not exist" in blocked["translation_placeholders"]["reason"]
    hass.config_entries.async_update_entry.assert_not_called()
    # The sensor appears: the reason clears and the offer takes its place.
    states["sensor.late"] = ("kW", {})
    _refresh(hass, entry, fake, now=T0 + sb.MISSING_GRACE + timedelta(minutes=15))
    assert set(fake.issues) == {_offer_id("solver_battery_power_sensor")}


def test_a_wrong_unit_only_plan_is_visible_at_once_and_recovers():
    fake = _FakeIR()
    states = {"sensor.batt_energy": ("kWh", {}), "sensor.batt": ("kW", {})}
    hass = _hass(states)
    entry = _entry({"battery_sensor": "sensor.batt_energy"})
    _refresh(hass, entry, fake)
    assert set(fake.issues) == {_blocked_id("solver_battery_power_sensor")}
    hass.config_entries.async_update_entry.assert_not_called()
    entry.options = {"battery_sensor": "sensor.batt"}  # the household corrects it
    _refresh(hass, entry, fake)
    assert set(fake.issues) == {_offer_id("solver_battery_power_sensor")}


def test_repeated_refreshes_do_not_duplicate_or_write():
    fake = _FakeIR()
    hass = _hass(TESTER_STATES)
    entry = _entry(_options("tester_2026_10_06.json"))
    for i in range(4):
        _refresh(hass, entry, fake, now=T0 + timedelta(minutes=15 * i))
    assert len(fake.issues) == 3
    hass.config_entries.async_update_entry.assert_not_called()


def test_an_offer_goes_away_once_the_field_is_set_another_way():
    fake = _FakeIR()
    hass = _hass(TESTER_STATES)
    entry = _entry(_options("tester_2026_10_06.json"))
    _refresh(hass, entry, fake)
    entry.options = {**entry.options, "solver_battery_power_sensor": "sensor.mine"}
    _refresh(hass, entry, fake)
    assert _offer_id("solver_battery_power_sensor") not in fake.issues


def test_the_battery_offer_states_the_sign_the_solver_applies():
    """Opposite-sign telemetry cannot be seen from a unit: the offer says
    which sign the Solver will apply, and points at the setting."""
    states = {"sensor.batt": ("kW", {})}
    for positive_is_charge, word in ((False, "discharging"), (True, "charging")):
        fake = _FakeIR()
        options = {
            "battery_sensor": "sensor.batt",
            "solver_battery_power_positive_is_charge": positive_is_charge,
        }
        _refresh(_hass(states), _entry(options), fake)
        sign = fake.issues[_offer_id("solver_battery_power_sensor")][
            "translation_placeholders"
        ]["sign"]
        assert f"**{word}**" in sign
        assert "does not change it" in sign


def test_refresh_never_raises():
    hass = _hass({})
    hass.states.get.side_effect = RuntimeError("boom")
    plan = _refresh(hass, _entry({"battery_sensor": "sensor.x"}), _FakeIR())
    assert plan.empty


# --- async_confirm_setup_fill: the household's Submit -------------------------


def _confirm_hass(states, options):
    hass = _hass(states)
    entry = _entry(options)
    hass.config_entries.async_get_entry.side_effect = lambda eid: (
        entry if eid == "e1" else None
    )

    def update(e, *, options):
        e.options = options

    hass.config_entries.async_update_entry.side_effect = update
    return hass, entry


def _confirm(hass, target, sensor, entry_id="e1"):
    return asyncio.run(sb.async_confirm_setup_fill(hass, entry_id, target, sensor))


def test_submit_sets_exactly_one_field_and_records_it():
    options = _options("tester_2026_10_06.json")
    hass, entry = _confirm_hass(TESTER_STATES, options)
    assert _confirm(hass, "solver_battery_power_sensor", "sensor.battery_power_1_2")
    hass.config_entries.async_update_entry.assert_called_once()
    written = entry.options
    assert written["solver_battery_power_sensor"] == "sensor.battery_power_1_2"
    assert written["setup_builder_done"] == ["option:solver_battery_power_sensor"]
    changed = {k for k in written if written[k] != options.get(k)}
    assert changed == {"solver_battery_power_sensor", "setup_builder_done"}


def test_submit_never_changes_the_sign_setting():
    options = {
        "battery_sensor": "sensor.batt",
        "solver_battery_power_positive_is_charge": True,
    }
    hass, entry = _confirm_hass({"sensor.batt": ("kW", {})}, options)
    assert _confirm(hass, "solver_battery_power_sensor", "sensor.batt")
    assert entry.options["solver_battery_power_positive_is_charge"] is True


def test_a_stale_submit_never_overwrites():
    options = {
        "battery_sensor": "sensor.batt",
        "solver_battery_power_sensor": "sensor.set_meanwhile",
    }
    hass, _ = _confirm_hass({"sensor.batt": ("kW", {})}, options)
    assert not _confirm(hass, "solver_battery_power_sensor", "sensor.batt")
    hass.config_entries.async_update_entry.assert_not_called()


def test_a_submit_for_a_sensor_that_turned_into_an_energy_total_is_refused():
    hass, _ = _confirm_hass(
        {"sensor.batt": ("kWh", {})}, {"battery_sensor": "sensor.batt"}
    )
    assert not _confirm(hass, "solver_battery_power_sensor", "sensor.batt")
    hass.config_entries.async_update_entry.assert_not_called()


def test_a_submit_for_another_sensor_or_unknown_field_or_entry_is_refused():
    hass, _ = _confirm_hass(
        {"sensor.batt": ("kW", {})}, {"battery_sensor": "sensor.batt"}
    )
    assert not _confirm(hass, "solver_battery_power_sensor", "sensor.other")
    assert not _confirm(hass, "solver_import_price_sensor", "sensor.batt")
    assert not _confirm(hass, "solver_battery_power_sensor", "sensor.batt", "e2")
    hass.config_entries.async_update_entry.assert_not_called()


def test_partial_solar_is_never_filled_without_a_submit():
    """A kW unit cannot prove a solar sensor is the site total (Mark,
    #1587). The refresh offers it with that warning and writes nothing."""
    fake = _FakeIR()
    hass = _hass({"sensor.inverter_1_pv": ("kW", {})})
    _refresh(hass, _entry({"solar_sensor": "sensor.inverter_1_pv"}), fake)
    assert _offer_id("solver_solar_power_sensor") in fake.issues
    hass.config_entries.async_update_entry.assert_not_called()
    strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    text = strings["issues"]["setup_fill_solar_power"]["fix_flow"]["step"]["confirm"][
        "description"
    ]
    assert "**total**" in text


# --- translations ---------------------------------------------------------------


def test_every_placeholder_in_the_texts_is_provided():
    provided = {
        "setup_fill_battery_power": {"field", "sensor", "reading", "sign"},
        "setup_fill_solar_power": {"field", "sensor", "reading"},
        "setup_fill_cross_check": {"field", "sensor", "reading"},
        "setup_fill_blocked": {"field", "sensor", "reason"},
    }
    for rel in ("strings.json", "translations/en.json"):
        issues = json.loads((PKG / rel).read_text(encoding="utf-8"))["issues"]
        for kind, names in provided.items():
            text = json.dumps(issues[kind])
            assert set(re.findall(r"\{(\w+)\}", text)) <= names, (rel, kind)
        for kind in ("setup_fill_battery_power", "setup_fill_solar_power"):
            assert "description" not in issues[kind]  # fixable: fix_flow only
            assert "no_longer_applies" in issues[kind]["fix_flow"]["abort"]


def test_the_repairs_platform_routes_offers_to_the_setup_fill_flow():
    source = (PKG / "repairs.py").read_text(encoding="utf-8")
    assert "async def async_create_fix_flow" in source
    assert "FIX_FLOW_KIND" in source
    assert "async_confirm_setup_fill" in source
