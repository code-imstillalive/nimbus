"""DC-005 step 2: provider profiles over the Energy Dashboard proposal
(device_discovery.add_provider_profiles). Synthetic entity ids only."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
sys.path.insert(0, str(_PKG))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dc = _load("device_contract")
dr = _load("device_resolver")
dd = _load("device_discovery")

LV = {
    "profile": "localvolts_v2",
    "instance": "entry_lv",
    "keys": {
        "buy_flex_up": "sensor.lv_buy",
        "sell_flex_up": "sensor.lv_sell",
        "current_buy_rate": "sensor.lv_current_buy",
        "current_sell_rate": "sensor.lv_current_sell",
        "p2p_settlement_history": "sensor.lv_p2p_history",
    },
}
AMBER_EXPRESS = {
    "profile": "amber_express",
    "instance": "site_a",
    "keys": {
        "general_price": "sensor.ae_general",
        "feed_in_price": "sensor.ae_feed_in",
    },
}
AMBER_CORE = {
    "profile": "amberelectric",
    "instance": "site_b",
    "keys": {
        "forecasts-general": "sensor.ac_general_forecast",
        "forecasts-feed_in": "sensor.ac_feed_in_forecast",
    },
}


def _grid(proposal):
    return next(d for d in proposal.document["devices"] if d["kind"] == "grid")


def _empty():
    return dd.propose_from_energy_dashboard(None)


def test_dc_r05_localvolts_proposes_each_price_role_on_the_grid():
    p = dd.add_provider_profiles(_empty(), [LV])
    b = _grid(p)["bindings"]
    assert {r: x["source"]["entity_id"] for r, x in b.items()} == {
        "import_price": "sensor.lv_buy",
        "export_price": "sensor.lv_sell",
        "import_price_forecast": "sensor.lv_current_buy",
        "export_price_forecast": "sensor.lv_current_sell",
    }
    fc = b["export_price_forecast"]
    assert fc["source"] == {
        "kind": "entity_attribute",
        "entity_id": "sensor.lv_current_sell",
        "attribute_path": "forecast",
        "adapter": {
            "name": "localvolts_v2",
            "version": 1,
            "field": "flexUp",
            "direction": "Sell",
        },
    }
    assert fc["normalization"] == {"quality": "forecast"}
    assert fc["provenance"]["origin"] == "provider_profile"
    assert fc["provenance"]["confirmed"] is False
    assert fc["provenance"]["evidence"] == [
        {
            "kind": "provider_profile",
            "profile": "localvolts_v2",
            "profile_version": 1,
            "instance": "entry_lv",
            "key": "current_sell_rate",
        }
    ]
    assert p.questions == [] and p.problems == []


def test_the_proposed_adapters_are_ones_dc002_resolves():
    """A profile never names an adapter DC-002 cannot read
    (adapter_unknown)."""
    db = _load("device_bindings")
    for profile, rows in dd.PROFILES.items():
        for _role, _key, path, adapter in rows:
            if path is not None:
                assert (adapter["name"], adapter["version"]) in db.ADAPTERS, profile


def test_the_p2p_matched_rate_and_settlement_are_reported_not_bound():
    """DC-001 has no role of their own yet (DC-002's open question 2)."""
    p = dd.add_provider_profiles(_empty(), [LV])
    assert [
        (f["code"], f["key"])
        for f in p.findings
        if f["code"] == "profile_key_has_no_role"
    ] == [("profile_key_has_no_role", "p2p_settlement_history")]
    bound = {x["source"]["entity_id"] for x in _grid(p)["bindings"].values()}
    assert "sensor.lv_p2p_history" not in bound


def test_two_profiles_for_one_role_are_a_question_not_a_ranking():
    """Decision 1: strength never breaks a disagreement."""
    p = dd.add_provider_profiles(_empty(), [LV, AMBER_EXPRESS])
    b = _grid(p)["bindings"]
    for role in (
        "import_price",
        "export_price",
        "import_price_forecast",
        "export_price_forecast",
    ):
        assert role not in b
    asked = {q["role"]: q for q in p.questions}
    assert set(asked) == {
        "import_price",
        "export_price",
        "import_price_forecast",
        "export_price_forecast",
    }
    # Profiles are taken in name order, so the listing never depends on the
    # order the HA side found them in.
    assert [c["source"] for c in asked["import_price"]["candidates"]] == [
        "sensor.ae_general",
        "sensor.lv_buy",
    ]
    reversed_order = dd.add_provider_profiles(_empty(), [AMBER_EXPRESS, LV])
    assert reversed_order.questions == p.questions


def test_the_same_price_from_both_is_corroboration_with_both_evidences():
    prefs = {
        "energy_sources": [
            {"type": "grid", "entity_energy_price": "sensor.lv_buy"},
        ]
    }
    p = dd.add_provider_profiles(dd.propose_from_energy_dashboard(prefs), [LV])
    b = _grid(p)["bindings"]["import_price"]
    assert [e["kind"] for e in b["provenance"]["evidence"]] == [
        "energy_dashboard",
        "provider_profile",
    ]
    assert not [q for q in p.questions if q["role"] == "import_price"]


def test_a_role_step_1_asked_about_stays_asked_with_the_profile_added():
    """A profile's source joins an existing question; it never settles it."""
    prefs = {
        "energy_sources": [
            {"type": "grid", "entity_energy_price": "sensor.a"},
            {"type": "grid", "entity_energy_price": "sensor.b"},
        ]
    }
    step1 = dd.propose_from_energy_dashboard(prefs)
    assert [q["role"] for q in step1.questions] == ["import_price"]
    p = dd.add_provider_profiles(step1, [LV])
    assert "import_price" not in _grid(p)["bindings"]
    (q,) = [q for q in p.questions if q["role"] == "import_price"]
    assert [c["source"] for c in q["candidates"]] == [
        "sensor.a",
        "sensor.b",
        "sensor.lv_buy",
    ]
    # And a profile naming one of the asked sources adds no duplicate.
    same = {
        "profile": "amber_express",
        "instance": "s",
        "keys": {"general_price": "sensor.a"},
    }
    p = dd.add_provider_profiles(step1, [same])
    (q,) = [q for q in p.questions if q["role"] == "import_price"]
    assert [c["source"] for c in q["candidates"]] == ["sensor.a", "sensor.b"]


def test_two_localvolts_accounts_propose_nothing_and_say_why():
    other = copy.deepcopy(LV)
    other["instance"] = "entry_lv_2"
    p = dd.add_provider_profiles(_empty(), [LV, other])
    assert _grid(p)["bindings"] == {}
    assert [(f["code"], f["instances"]) for f in p.findings if f.get("profile")] == [
        ("profile_several_instances", ["entry_lv", "entry_lv_2"])
    ]


def test_recognised_providers_with_no_role_yet_are_reported():
    detected = [
        {"profile": "aemo_nem", "instance": "e1", "keys": {}},
        {"profile": "nem_pd7day", "instance": "e2", "keys": {}},
        {"profile": "openadr3_ven", "instance": "e3", "keys": {}},
        {"profile": "someone_elses", "instance": "e4", "keys": {}},
    ]
    p = dd.add_provider_profiles(_empty(), detected)
    assert _grid(p)["bindings"] == {}
    assert sorted((f["code"], f["profile"]) for f in p.findings if "profile" in f) == [
        ("profile_reported_only", "aemo_nem"),
        ("profile_reported_only", "nem_pd7day"),
        ("profile_reported_only", "openadr3_ven"),
        ("profile_unknown", "someone_elses"),
    ]
