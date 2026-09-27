"""Spec 000: the golden scenarios reach the paths they exist for.

An exact snapshot of an early return is still an exact snapshot, so each
scenario's purpose is asserted separately, against its committed snapshot
(fast; ``test_golden_master.py`` proves the snapshot is current). Part D
also checks that the committed NEMWEB-derived inputs are the files their
provenance names.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
SPIKE = 5.0  # $/kWh: well above any retail price, well below the $23.32 cap

NEMWEB_FOLDERS = sorted(p.name for p in (GOLDEN / "nemweb").iterdir() if p.is_dir())


def _snapshot(name: str) -> dict:
    return json.loads(
        gzip.decompress((GOLDEN / "snapshots" / f"{name}.json.gz").read_bytes())
    )


def _battery(cycle: dict) -> dict:
    (post,) = [
        p
        for p in cycle["posted"]
        if p["entity_id"] == "sensor.nimbus_solver_battery_forecast"
    ]
    return post["attributes"]


def test_two_cycles_reads_back_the_plan() -> None:
    snap = _snapshot("guardrail_two_cycles")
    assert "plan_state.json" in snap["files"]
    first, second = (_battery(c) for c in snap["cycles"])
    # Same inputs, five minutes later: the only reason the LP dual moves
    # is the proximal term against the plan cycle 1 saved.
    assert first["forecast"][1]["shadow_price"] != second["forecast"][1]["shadow_price"]


# ---------------------------------------------------------------------------
# nimbus issue #1335: the native-mode scenarios reach build_controllable_loads
# and apply_commanded_state_guard, both of which return on their first line
# when _NATIVE_HASS is None. Each assertion below is 0/absent on the
# standalone path every other scenario takes, so none of them can be met by a
# scenario that merely enters those functions and hits the early return.
# ---------------------------------------------------------------------------

NATIVE_RUN_STATE = "store.nimbus_load_golden_hub_entry_load_run_state.json"


def _diagnostics(cycle: dict) -> dict:
    return _battery(cycle)["solve_diagnostics"]


def test_native_scenario_actually_took_the_native_seam() -> None:
    # Every read in native mode is hass.states.get / the recorder, never
    # urllib. A single REST request here would mean the scenario ran the
    # standalone path with controllable-load subentries nothing could see.
    for name in ("native_controllable_loads", "native_controllable_loads_two_cycles"):
        for cycle in _snapshot(name)["cycles"]:
            methods = {r["method"] for r in cycle["requests"]}
            assert methods, f"{name}: no reads recorded at all"
            assert methods <= {"NATIVE_GET", "NATIVE_HISTORY"}, (
                f"{name}: {sorted(methods - {'NATIVE_GET', 'NATIVE_HISTORY'})}"
            )


def test_native_scenario_builds_both_controllable_load_kinds() -> None:
    # build_controllable_loads() returned ([], [], []) for the whole life of
    # the golden master before #1335; these three counts are that return
    # value, as main() publishes it.
    diag = _diagnostics(_snapshot("native_controllable_loads")["cycles"][0])
    assert diag["n_controllable_loads"] == 2
    assert diag["n_sheddable_loads"] == 1
    assert diag["n_adequacy_loads"] == 1


def test_native_scenario_dispatches_both_loads_through_the_guard() -> None:
    # apply_commanded_state_guard()'s own output stage: one service call per
    # load, each the right service for its device entity's domain.
    cycle = _snapshot("native_controllable_loads")["cycles"][0]
    calls = {(c["domain"], c["service"]): c["data"] for c in cycle["service_calls"]}
    assert calls[("switch", "turn_on")] == {"entity_id": "switch.golden_pool_pump"}
    assert calls[("water_heater", "set_operation_mode")] == {
        "entity_id": "water_heater.golden_hws",
        "operation_mode": "performance",
    }


def test_native_scenario_persists_a_real_run_state_for_each_load() -> None:
    snap = _snapshot("native_controllable_loads")
    store = snap["files"][NATIVE_RUN_STATE]
    assert set(store) == {"golden_sheddable", "golden_deferrable"}
    for key, state in store.items():
        assert state["commanded_state"] is True, key
        assert state["activations_today"] == 1, key
        # The full day-ahead per-load plan (#581), not just period 0.
        assert len(state["plan_forecast"]) == 202, key
    # The deferrable load's power sensor is not configured on its subentry:
    # #768's device-registry discovery resolved it, and the W-reporting
    # sensor was scaled to kW (2950 W -> 2.95 kW, well over the 0.05 kW
    # on-threshold), so this is a real reading and not a default.
    assert store["golden_deferrable"]["currently_on"] is True
    assert store["golden_deferrable"]["plan_target_kwh"] == 3.25


def test_native_two_cycles_does_not_re_dispatch_an_unchanged_command() -> None:
    first, second = _snapshot("native_controllable_loads_two_cycles")["cycles"]
    assert len(first["service_calls"]) == 2
    # Dispatch is change-gated. Cycle 2 wants the same thing, so nothing goes
    # out -- and the debounce/hold path is what decided that, which is only
    # reachable because cycle 1 persisted a state for it to read back.
    assert second["service_calls"] == []
    store = _snapshot("native_controllable_loads_two_cycles")["files"][NATIVE_RUN_STATE]
    for key, state in store.items():
        assert state["delivered_today_kwh"] > 0.0, key
        assert state["activations_today"] == 1, key


@pytest.mark.parametrize("folder", NEMWEB_FOLDERS)
def test_nemweb_inputs_match_their_provenance(folder: str) -> None:
    base = GOLDEN / "nemweb" / folder
    manifest = [
        json.loads(line) for line in (base / "manifest.jsonl").read_text().splitlines()
    ]
    assert {row["feed"] for row in manifest} >= {"pd7day", "stpasa", "tradingis"}
    assert all(row["url"].startswith("https://nemweb.com.au/") for row in manifest)
    for variant in ("passthrough", "fitted"):
        fc = json.loads(
            gzip.decompress(
                (base / f"nem_pd7day_forecast.{variant}.json.gz").read_bytes()
            )
        )
        for name, sha in fc["provenance"]["inputs"].items():
            assert hashlib.sha256((base / name).read_bytes()).hexdigest() == sha, name
        sources = {p["calibrated_source"] for p in fc["attributes"]["forecast"]}
        assert sources == (
            {"passthrough"} if variant == "passthrough" else sources - {"passthrough"}
        )


@pytest.mark.parametrize("folder", NEMWEB_FOLDERS)
def test_pd7day_spikes_reach_the_lp_uncalibrated_and_not_calibrated(
    folder: str,
) -> None:
    raw = _battery(_snapshot(f"{folder}_passthrough")["cycles"][0])
    spikes = [p for p in raw["forecast"] if p["import_price"] >= SPIKE]
    assert spikes, "no PD7DAY spike reached import_price"
    assert any(p["battery_kw"] > 0 for p in spikes), (
        "battery never discharges into a spike"
    )

    # Beyond the first 30 minutes every price is a PD7DAY forecast; inside
    # them the live price and the retail array decide (the real SA1 spike
    # in sa_nemweb_spike_arrived is live, not forecast).
    snap = _snapshot(f"{folder}_fitted")
    horizon = datetime.fromisoformat(snap["cycles"][0]["instant"]) + timedelta(
        minutes=30
    )
    fitted = _battery(snap["cycles"][0])
    late = [
        p
        for p in fitted["forecast"]
        if datetime.fromisoformat(p["time"]) >= horizon and p["import_price"] >= SPIKE
    ]
    assert not late, "calibration left a forecast spike in import_price"


@pytest.mark.parametrize("variant", ["passthrough", "fitted"])
def test_real_spike_arms_the_override(variant: str) -> None:
    attrs = _battery(_snapshot(f"sa_nemweb_spike_arrived_{variant}")["cycles"][0])
    assert attrs["price_spike_active"] is True
    assert attrs["forecast"][0]["import_price"] >= SPIKE
    assert attrs["forecast"][0]["battery_kw"] == 5.0


def test_forecast_spikes_alone_do_not_arm_the_override() -> None:
    for folder in NEMWEB_FOLDERS:
        if folder == "sa_nemweb_spike_arrived":
            continue
        attrs = _battery(_snapshot(f"{folder}_passthrough")["cycles"][0])
        assert attrs["price_spike_active"] is False, folder
