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

    # test_every_profile_has_a_support_matrix_row
    # DC-008 (#1574): the support matrix in the user docs matches the profiles
    # in code. A profile, or a reported-only provider, without a row fails.
    import re as _re

    matrix = (
        Path(__file__).resolve().parents[2] / "docs" / "providers" / "support-matrix.md"
    ).read_text(encoding="utf-8")
    rows = dict(
        _re.findall(
            r"^\| [^|]+\| `([a-z0-9_]+)` \| ([a-z ]+?) \|", matrix, _re.MULTILINE
        )
    )
    assert set(dd.PROFILES) | set(dd.REPORTED_ONLY) == set(rows)
    for profile in dd.PROFILES:
        assert rows[profile] in ("supported", "proposed"), profile
    for provider in dd.REPORTED_ONLY:
        assert rows[provider] == "reported only", provider


SIGEN = {
    "profile": "sigen",
    "instance": "entry_sigen",
    "keys": {
        "sigen_inverter_inverter_ess_charge_discharge_power": "sensor.sig_bat_power",
        "sigen_plant_plant_ess_soc": "sensor.sig_soc",
        "sigen_plant_plant_photovoltaic_power": "sensor.sig_pv",
        "sigen_plant_plant_total_load_power": "sensor.sig_total_load",
        "sigen_plant_plant_consumed_power": "sensor.sig_consumed",
        "sigen_plant_plant_grid_import_power": "sensor.sig_grid_in",
        "sigen_plant_plant_grid_export_power": "sensor.sig_grid_out",
    },
}


def _device(proposal, ident):
    return next(d for d in proposal.document["devices"] if d["id"] == ident)


def test_a_sigenergy_plant_fills_battery_solar_whole_house_and_grid():
    """#2514 (DC-R17, keys from a real Sigenergy site via Mark Purcell): one
    equipment profile fills the battery, solar and Whole house devices, with
    the battery power's own sign, and the Grid's separate import and export
    power; the second whole-house meter is reported,
    not bound; an Energy Dashboard source that agrees is corroborated, and one
    that disagrees, even on sign alone, is a question, never overridden."""
    p = dd.add_provider_profiles(_empty(), [SIGEN])
    battery = _device(p, "battery")
    assert battery["bindings"]["power"]["source"]["entity_id"] == "sensor.sig_bat_power"
    assert battery["bindings"]["power"]["normalization"] == {
        "positive_direction": "discharge",
        "native_positive_direction": "charge",
    }
    assert battery["bindings"]["soc"]["source"]["entity_id"] == "sensor.sig_soc"
    assert any(
        q["code"] == dd.Q_FACT_NEEDED and q["device"] == "battery" for q in p.questions
    )
    solar = _device(p, "solar")
    assert solar["bindings"]["power"]["source"]["entity_id"] == "sensor.sig_pv"
    house = _device(p, "load_house")
    assert house["bindings"]["power"]["source"]["entity_id"] == "sensor.sig_total_load"
    assert house["bindings"]["power"]["normalization"]["native_positive_direction"] == (
        "consumption"
    )
    assert [
        (f["code"], f["entity_id"]) for f in p.findings if f.get("profile") == "sigen"
    ] == [(dd.F_PROFILE_NO_ROLE, "sensor.sig_consumed")]
    assert all(f["code"] != dd.F_PROFILE_UNKNOWN for f in p.findings)
    grid = _grid(p)["bindings"]
    # No net grid sensor: the net is derived as import minus export.
    assert sorted(grid) == ["export_power", "import_power", "power"]
    assert grid["power"]["source"] == {
        "kind": "derived",
        "calculation": "difference",
        "depends_on": ["grid.import_power", "grid.export_power"],
    }
    assert grid["power"]["provenance"]["origin"] == "derived"
    # The derived net validates; the only problem is the battery's capacity,
    # which discovery asks for as a question.
    assert [x.code for x in p.problems] == ["capacity_missing"]
    assert grid["import_power"]["source"]["entity_id"] == "sensor.sig_grid_in"
    assert grid["export_power"]["source"]["entity_id"] == "sensor.sig_grid_out"
    assert grid["export_power"]["normalization"] == {
        "positive_direction": "export",
        "native_positive_direction": "export",
    }

    # With an Energy Dashboard: the same solar sensor and sign is corroborated;
    # the same battery sensor with the opposite sign is a question; the
    # dashboard's net grid power stays, beside the profile's import and export.
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.grid_in_kwh",
                "stat_energy_to": "sensor.grid_out_kwh",
                "stat_rate": "sensor.grid_net",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv_kwh",
                "stat_rate": "sensor.sig_pv",
            },
            {
                "type": "battery",
                "stat_energy_from": "sensor.bat_out_kwh",
                "stat_energy_to": "sensor.bat_in_kwh",
                "stat_rate": "sensor.sig_bat_power",
            },
        ]
    }
    p = dd.add_provider_profiles(dd.propose_from_energy_dashboard(prefs), [SIGEN])
    solar = _device(p, "solar")
    kinds = [e["kind"] for e in solar["bindings"]["power"]["provenance"]["evidence"]]
    assert "provider_profile" in kinds and len(kinds) == 2
    grid = _grid(p)["bindings"]
    assert grid["power"]["source"]["entity_id"] == "sensor.grid_net"
    assert grid["import_power"]["source"]["entity_id"] == "sensor.sig_grid_in"
    battery = _device(p, "battery")
    assert "power" not in battery["bindings"]
    (q,) = [
        q
        for q in p.questions
        if q["code"] == dd.Q_SEVERAL_SOURCES and q["device"] == "battery"
    ]
    assert q["role"] == "power"
    assert sorted(
        c["normalization"]["native_positive_direction"] for c in q["candidates"]
    ) == [
        "charge",
        "discharge",
    ]

    # Two Sigenergy plants: nothing is proposed rather than guessed.
    two = dict(SIGEN, instance="entry_sigen_2")
    p = dd.add_provider_profiles(_empty(), [SIGEN, two])
    assert not any(d["kind"] == "battery" for d in p.document["devices"])
    assert any(
        f["code"] == dd.F_PROFILE_SEVERAL and f["profile"] == "sigen"
        for f in p.findings
    )
