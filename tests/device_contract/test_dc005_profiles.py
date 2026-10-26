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


def test_the_proposed_adapters_are_ones_dc002_resolves():
    # test_the_proposed_adapters_are_ones_dc002_resolves: A profile never names an adapter DC-002 cannot read
    db = _load("device_bindings")
    for profile, rows in dd.PROFILES.items():
        for _role, _key, path, adapter in rows:
            if path is not None:
                assert (adapter["name"], adapter["version"]) in db.ADAPTERS, profile

    # test_the_energy_dashboards_price_and_a_profiles_differ_so_it_is_asked
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.in_kwh",
                "entity_energy_price": "sensor.my_buy",
            }
        ]
    }
    p = dd.add_provider_profiles(dd.propose_from_energy_dashboard(prefs), [LV])
    assert "import_price" not in _grid(p)["bindings"]
    (q,) = [q for q in p.questions if q["role"] == "import_price"]
    assert [c["source"] for c in q["candidates"]] == ["sensor.my_buy", "sensor.lv_buy"]
    # Energy is untouched by a price profile.
    assert _grid(p)["bindings"]["import_energy"]["source"]["statistic_id"] == (
        "sensor.in_kwh"
    )

    # test_the_same_price_from_both_is_corroboration_with_both_evidences
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

    # test_a_role_step_1_asked_about_stays_asked_with_the_profile_added: A profile's source joins an existing question; it never settles it.
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

    # test_two_localvolts_accounts_propose_nothing_and_say_why
    other = copy.deepcopy(LV)
    other["instance"] = "entry_lv_2"
    p = dd.add_provider_profiles(_empty(), [LV, other])
    assert _grid(p)["bindings"] == {}
    assert [(f["code"], f["instances"]) for f in p.findings if f.get("profile")] == [
        ("profile_several_instances", ["entry_lv", "entry_lv_2"])
    ]

    # test_recognised_providers_with_no_role_yet_are_reported
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

    # test_registry_ids_kept_and_input_untouched
    base = _empty()
    before = copy.deepcopy(base.document)
    p = dd.add_provider_profiles(
        base, [LV], registry_ids={"sensor.lv_current_sell": "reg_sell"}
    )
    assert (
        _grid(p)["bindings"]["export_price_forecast"]["source"]["registry_id"]
        == "reg_sell"
    )
    assert base.document == before
