"""nimbus #1797 (PR C): the closed-loop paired replay, `scripts/stochastic_replay.py`.

Every export here is built through the REAL capture (`decision_inputs.
build_record`), so the replay is pinned to the schema the service returns.
Synthetic data only, no household telemetry.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from custom_components.nimbus_load import decision_inputs as di
from custom_components.nimbus_load import solver_publish


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "stochastic_replay", REPO / "scripts" / "stochastic_replay.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["stochastic_replay"] = module
    spec.loader.exec_module(module)
    return module


rp = _load_tool()

T0 = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)  # 06:00 AEST
CONFIG = {
    "solver_battery_capacity_kwh": 20.0,
    "solver_max_charge_kw": 5.0,
    "solver_max_discharge_kw": 5.0,
    "solver_battery_min_soc_percent": 10,
    "solver_battery_max_soc_percent": 100,
    "solver_efficiency_percent": 90,
    "solver_salvage_value": 0.10,
}


def _local_h(t: datetime) -> float:
    return ((t - T0).total_seconds() / 3600 + 6) % 24


def _sun(h: float) -> float:
    return max(0.0, 6.0 * math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0


def _buy(h: float) -> float:
    return 0.40 if 16 <= h < 21 else (0.08 if 9 <= h < 15 else 0.25)


def _sell(h: float) -> float:
    return 0.30 if 16 <= h < 21 else 0.03


def _rows(t, n, *, band=True, effective=True, actual_share=0.35):
    out = []
    for i in range(n):
        ts = t + timedelta(hours=i * 0.5)
        h = _local_h(ts)
        f = _sun(h)
        row = {
            "time": ts.isoformat(),
            "hours": 0.5,
            "import_price": _buy(h),
            "export_price": _sell(h),
            "load_kw": 3.0 if 17 <= h < 22 else 1.0,
            "solar_kw": actual_share * f if i == 0 else f,
            "envelope_import_limit_kw": 15.0,
            "envelope_export_limit_kw": 10.0,
            "soc_pct": 50.0,
        }
        if band:
            row["solar_p10_kw"] = 0.3 * f
            row["solar_p90_kw"] = min(1.2 * f, 7.5)
        if effective:
            row["solar_effective_kw"] = 0.75 * f
            row["import_price_effective"] = _buy(h)
            row["export_price_effective"] = _sell(h)
        out.append(row)
    return out


def _export(hours=18.0, **kw) -> dict:
    """Head records every 5 minutes, a full horizon every 30, like the store."""
    records = []
    t = T0
    while t < T0 + timedelta(hours=hours):
        kind = "horizon" if (t - T0).total_seconds() % 1800 == 0 else "head"
        rows = _rows(t, 48 if kind == "horizon" else 4, **kw)
        records.append(
            di.build_record(
                {"forecast": rows, "status": "optimal"},
                captured_at=t,
                kind=kind,
                published_at=t.isoformat(),
                config_hash="cfg",
                sources=[],
            )
        )
        t += timedelta(minutes=5)
    return {"records": records, "configs": {"cfg": dict(CONFIG)}}


# ---- capture -----------------------------------------------------------------


def test_capture_keeps_the_effective_inputs():
    """The fields the replay's `main` arm rebuilds the live plan from are
    captured with every record."""
    for field in (
        "solar_effective_kw",
        "import_price_effective",
        "export_price_effective",
    ):
        assert field in di.ROW_FIELDS
    record = _export(hours=0.5)["records"][0]
    assert record["rows"]["solar_effective_kw"][1] == pytest.approx(0.75 * _sun(6.5))


def test_published_row_effective_value():
    """`_effective_at` publishes what the LP counted on, rounded, and None
    on a plan built without the arrays."""
    assert solver_publish._effective_at(np.array([1.23456, 2.0]), 0, 3) == 1.235
    assert solver_publish._effective_at(np.zeros(0), 0, 3) is None
    assert solver_publish._effective_at(None, 0, 3) is None


# ---- reading -----------------------------------------------------------------


def test_merge_exports_counts_a_repeated_record_once():
    one = _export(hours=1)
    records, configs = rp.merge_exports([one, one])
    assert len(records) == len(one["records"])
    assert configs == {"cfg": CONFIG}


def test_readings_skip_an_incomplete_row_zero():
    export = _export(hours=0.5)
    export["records"][1]["rows"]["load_kw"][0] = None
    found = rp.readings(export["records"])
    assert len(found) == len(export["records"]) - 1
    assert all(r.solar_kw >= 0 for r in found)


def test_a_long_gap_is_settled_for_no_arm():
    found = [
        rp.Reading(T0, 0, 1, 0.2, 0.05),
        rp.Reading(T0 + timedelta(minutes=5), 0, 1, 0.2, 0.05),
        rp.Reading(T0 + timedelta(minutes=50), 0, 1, 0.2, 0.05),
        rp.Reading(T0 + timedelta(minutes=55), 0, 1, 0.2, 0.05),
    ]
    settled, gaps = rp.intervals(found, timedelta(minutes=15))
    assert gaps == 1
    assert [iv.start for iv in settled] == [found[0].at, found[2].at]
    assert settled[0].hours == pytest.approx(5 / 60)


def test_battery_spec_from_the_config():
    spec = rp.battery_spec({**CONFIG, "solver_battery_soh_percent": 90})
    assert spec.capacity_kwh == pytest.approx(18.0)
    assert spec.min_soc_kwh == pytest.approx(1.8)
    assert spec.leg_efficiency == pytest.approx(0.9**0.5)
    assert rp.battery_spec({"solver_max_charge_kw": 5}) is None


def test_a_record_missing_a_column_is_no_decision():
    record = _export(hours=0.5)["records"][0]
    del record["rows"]["envelope_export_limit_kw"]
    assert rp.decision(record, CONFIG) is None


def test_each_arm_gets_its_own_solar():
    d = rp.decision(_export(hours=0.5)["records"][0], CONFIG)
    assert d is not None and d.calibrated_band
    assert np.allclose(d.solar("main").forecast_kw, d.effective_solar_kw)
    assert np.allclose(d.solar("p50").forecast_kw, d.solar_kw)
    stochastic = d.solar("stochastic")
    assert stochastic.p10_kw is not None and stochastic.p90_kw is not None
    assert np.allclose(stochastic.forecast_kw, d.solar_kw)


# ---- settlement --------------------------------------------------------------


def _iv(solar=0.0, load=1.0, buy=0.2, sell=0.05, hours=0.5):
    return rp.Interval(T0, hours, rp.Reading(T0, solar, load, buy, sell))


def test_settle_balances_the_grid_and_prices_it():
    spec = rp.battery_spec(CONFIG)
    s = rp.settle(-2.0, spec, 10.0, _iv(solar=0.5, load=1.0), 15.0, 10.0)
    assert s.import_kw == pytest.approx(2.5)  # 1.0 load - 0.5 solar + 2.0 charge
    assert s.money == pytest.approx(0.5 * 2.5 * 0.2)
    assert s.soc_kwh == pytest.approx(10.0 + 2.0 * spec.leg_efficiency * 0.5)


def test_settle_stops_at_the_soc_floor_and_ceiling():
    spec = rp.battery_spec(CONFIG)
    empty = rp.settle(5.0, spec, spec.min_soc_kwh, _iv(), 15.0, 10.0)
    assert empty.battery_kw == 0.0 and empty.soc_kwh == pytest.approx(spec.min_soc_kwh)
    full = rp.settle(-5.0, spec, spec.max_soc_kwh, _iv(), 15.0, 10.0)
    assert full.battery_kw == 0.0


def test_settle_curtails_above_the_export_limit_and_cuts_charge_at_import():
    spec = rp.battery_spec(CONFIG)
    s = rp.settle(0.0, spec, 10.0, _iv(solar=14.0, load=1.0), 15.0, 10.0)
    assert s.export_kw == pytest.approx(10.0) and s.curtailed_kw == pytest.approx(3.0)
    s = rp.settle(-5.0, spec, 10.0, _iv(load=13.0), 15.0, 10.0)
    assert s.import_kw == pytest.approx(15.0) and s.battery_kw == pytest.approx(-2.0)


# ---- the loop and the comparison ---------------------------------------------


def test_two_arms_on_identical_inputs_pair_to_exactly_zero():
    """Without the effective fields `main` IS `p50`: the harness must show
    no difference at all, or it is manufacturing one."""
    out = rp.replay(
        [_export(hours=4, effective=False)], arms=["main", "p50"], soc0_pct=50
    )
    assert out["raw_input_records"] == out["horizon_records"]
    (c,) = out["comparisons"]
    assert c["grid_money_delta"] == 0.0 and c["total_delta"] == 0.0
    assert out["arms"]["main"]["grid_money"] == out["arms"]["p50"]["grid_money"]


def test_without_a_band_stochastic_is_the_main_plan_exactly():
    """Drive refuses without a band and publishes the main plan; so does the
    replay, interval for interval."""
    out = rp.replay(
        [_export(hours=4, band=False)], arms=["main", "stochastic"], soc0_pct=50
    )
    arm = out["arms"]["stochastic"]
    assert arm["plans_used"] == {"main": out["horizon_records"]}
    assert arm["fallback_reasons"] == {"no_solar_band": out["horizon_records"]}
    assert arm["grid_money"] == out["arms"]["main"]["grid_money"]


def test_the_hedge_pays_on_a_day_the_sun_does_not_come():
    """A sunny forecast, a cloudy day (35% of p50) and a p10 at 30%: the
    plan that weighed the low day buys more cheap midday power, and pays
    less than the plan that trusted p50. The main plan's 25% solar derate
    recovers part of that; the comparisons report each step separately."""
    out = rp.replay([_export(hours=18)], soc0_pct=50)
    by = {c["arms"]: c for c in out["comparisons"]}
    assert out["arms"]["stochastic"]["plans_used"] == {
        "stochastic": out["horizon_records"]
    }
    assert by["stochastic - p50"]["total_delta"] < 0
    assert by["p50 - main"]["total_delta"] > 0  # the derate helps on this day
    assert by["stochastic - main"]["total_delta"] < 0


def test_the_end_soc_is_valued_the_same_for_every_arm():
    a = rp.ArmRun("a", money=[1.0, 1.0], soc_end_kwh=5.0)
    b = rp.ArmRun("b", money=[1.0, 1.0], soc_end_kwh=8.0)
    settled = [_iv(), _iv()]
    c = rp.compare(a, b, settled, UTC, salvage=0.10, block_hours=6)
    assert c["grid_money_delta"] == 0.0
    assert c["terminal_value_delta"] == pytest.approx(-0.30)  # 3 kWh more left
    assert c["total_delta"] == pytest.approx(-0.30)


def test_block_bootstrap():
    assert rp.block_bootstrap([0.1] * 5, block=3) is None
    lo, hi = rp.block_bootstrap([0.1] * 12, block=3)
    assert lo == pytest.approx(1.2) and hi == pytest.approx(1.2)


def test_nothing_to_replay_is_said_not_raised():
    out = rp.replay([{"records": [], "configs": {}}])
    assert out["error"] == "nothing to replay"


def test_cli_prints_and_writes_json(tmp_path, capsys):
    src = tmp_path / "export.json"
    src.write_text(json.dumps({"service_response": _export(hours=2)}), encoding="utf-8")
    out = tmp_path / "out.json"
    assert (
        rp.main(
            [str(src), "--arms", "main,p50", "--json", str(out), "--soc0-pct", "50"]
        )
        == 0
    )
    assert "p50 - main" in capsys.readouterr().out
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert set(saved["arms"]) == {"main", "p50"}


def test_cli_refuses_an_unknown_arm(tmp_path):
    src = tmp_path / "export.json"
    src.write_text(json.dumps(_export(hours=0.5)), encoding="utf-8")
    with pytest.raises(SystemExit):
        rp.main([str(src), "--arms", "oracle"])
