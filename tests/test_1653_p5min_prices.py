"""nimbus #1653: AEMO's 5-minute pre-dispatch priced into the solve.

No precharge rule exists to test: the overlay changes prices and nothing
else, and the last two tests show the LP itself charging ahead of a spike
it can see and not charging once the spike is withdrawn.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import p5min
from custom_components.nimbus_load.solver_inputs import p5min_prices as pp

AEST = timezone(timedelta(hours=10))
T0 = datetime(2026, 10, 9, 4, 30, tzinfo=AEST)
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "p5min"
    / "PUBLIC_P5MIN_202610091150_20261009114620.CSV"
)


def _rows(prices, start=T0):
    return [
        {
            "start": (start + timedelta(minutes=5 * k)).isoformat(),
            "end": (start + timedelta(minutes=5 * (k + 1))).isoformat(),
            "value": v,
        }
        for k, v in enumerate(prices)
    ]


def _grid(n=12, start=T0):
    return [start + timedelta(minutes=5 * k) for k in range(n)]


def _state(rows, run=T0):
    return {"attributes": {"run_datetime": run.isoformat(), "forecast": rows}}


# ---- the overlay ------------------------------------------------------------


def test_future_periods_take_wholesale_plus_their_own_bucket_markup():
    grid = _grid(4)
    values, sources = [0.10] * 4, ["primary"] * 4
    bucket = lambda t: t.hour * 12 + t.minute // 5
    offset = {bucket(t): 0.01 * (k + 1) for k, t in enumerate(grid)}
    changed = pp.overlay(
        values, sources, _rows([0.08, 0.70, 0.86, 0.09]), grid, grid[1], offset
    )
    assert changed == [1, 2, 3]
    assert values == [0.10, 0.72, 0.89, 0.13]
    assert sources == ["primary", "p5min", "p5min", "p5min"]


def test_the_settled_block_is_never_touched():
    grid = _grid(3)
    values, sources = [0.10] * 3, ["primary"] * 3
    pp.overlay(values, sources, _rows([0.9, 0.9, 0.9]), grid, grid[1], {0: 0.0})
    assert values[0] == 0.10 and sources[0] == "primary"


def test_periods_beyond_the_p5min_horizon_keep_their_price():
    grid = _grid(6)
    values, sources = [0.10] * 6, ["primary"] * 6
    pp.overlay(values, sources, _rows([0.5, 0.5]), grid, grid[0], {0: 0.0})
    assert values[2:] == [0.10] * 4


def test_a_one_minute_period_takes_the_interval_it_starts_in():
    grid = [T0 + timedelta(minutes=m) for m in (6, 7, 11)]
    values, sources = [0.0] * 3, [""] * 3
    pp.overlay(values, sources, _rows([0.1, 0.6, 0.7]), grid, T0, {0: 0.0})
    # every bucket missing -> the mean markup, 0.0
    assert values == [0.6, 0.6, 0.7]


def test_no_learned_markup_means_no_change():
    grid = _grid(2)
    values, sources = [0.10, 0.10], ["primary"] * 2
    assert pp.overlay(values, sources, _rows([0.9, 0.9]), grid, T0, {}) == []
    assert values == [0.10, 0.10]


def test_the_real_aemo_file_overlays_cleanly():
    run = p5min.parse_region_solution(FIXTURE.read_text(), "QLD1")
    start = datetime.fromisoformat(run["forecast"][1]["start"])
    grid = _grid(11, start)
    values, sources = [0.0] * 11, [""] * 11
    changed = pp.overlay(values, sources, run["forecast"], grid, start, {0: 0.0})
    assert changed == list(range(11))
    assert values == [r["value"] for r in run["forecast"][1:]]


def test_9_oct_the_0430_run_puts_the_spike_in_front_of_the_solve():
    """The real AEMO run published at 04:25:49 on 9 Oct 2026, the morning
    the battery sold at 04:30 and was empty through the spike."""
    text = (
        FIXTURE.parent / "2026-10-09" / "PUBLIC_P5MIN_202610090430_20261009042549.CSV"
    ).read_text()
    run = p5min.parse_region_solution(text, "QLD1")
    state = {
        "attributes": {"run_datetime": run["run_datetime"], "forecast": run["forecast"]}
    }
    rows = pp.fresh_rows(state, datetime(2026, 10, 9, 4, 31, tzinfo=AEST))
    grid = _grid(12, datetime(2026, 10, 9, 4, 30, tzinfo=AEST))
    values, sources = [0.25] * 12, ["primary"] * 12
    pp.overlay(values, sources, rows, grid, grid[1], {0: 0.0})
    by_time = {t.strftime("%H:%M"): v for t, v in zip(grid, values, strict=True)}
    assert by_time["04:55"] > 0.39
    assert by_time["05:20"] > 0.50


# ---- freshness and the no-op paths -----------------------------------------


def test_a_stale_run_is_ignored():
    rows = _rows([0.9])
    assert pp.fresh_rows(_state(rows), T0 + timedelta(minutes=15)) == rows
    assert pp.fresh_rows(_state(rows), T0 + timedelta(minutes=16)) == []


def test_missing_or_malformed_state_is_ignored():
    assert pp.fresh_rows(None, T0) == []
    assert pp.fresh_rows({"attributes": {"forecast": _rows([1])}}, T0) == []
    bad = {"attributes": {"run_datetime": "soon", "forecast": _rows([1])}}
    assert pp.fresh_rows(bad, T0) == []


def _cfg():
    return {
        "solver_import_price_sensor": "sensor.import",
        "solver_export_price_sensor": "sensor.export",
    }


class _Registry:
    def __init__(self, by_unique_id):
        self.by_unique_id = by_unique_id

    def async_get_entity_id(self, domain, platform, unique_id):
        assert (domain, platform) == ("sensor", "nimbus_load")
        return self.by_unique_id.get(unique_id)


def _native(monkeypatch, by_unique_id, entry_ids=("hub",)):
    from types import SimpleNamespace

    from homeassistant.helpers import entity_registry as er

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda _d: [SimpleNamespace(entry_id=e) for e in entry_ids]
        )
    )
    monkeypatch.setattr(pp.solver_shared.NATIVE, "hass", hass)
    monkeypatch.setattr(er, "async_get", lambda _h: _Registry(by_unique_id))


def test_the_entity_is_found_by_unique_id_not_by_its_plain_id(monkeypatch):
    """A mirrored install's sensor can sit on sensor.nimbus_p5min_forecast
    while this install's own is bumped to _2. The battery is priced off its
    own feed."""
    _native(monkeypatch, {"hub_p5min_forecast": "sensor.nimbus_p5min_forecast_2"})
    assert pp.resolve_entity_id() == "sensor.nimbus_p5min_forecast_2"


def test_no_registered_entity_is_a_non_nem_install(monkeypatch):
    _native(monkeypatch, {})
    assert pp.resolve_entity_id() is None


def test_outside_ha_there_is_no_overlay(monkeypatch):
    monkeypatch.setattr(pp.solver_shared.NATIVE, "hass", None)
    assert pp.resolve_entity_id() is None


def test_no_p5min_entity_is_a_no_op(monkeypatch):
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: None)
    imp, exp = [0.1] * 3, [0.05] * 3
    out = pp.apply(_cfg(), _grid(3), T0, T0, (imp, [""] * 3), (exp, [""] * 3), None)
    assert out == {"applied": False, "reason": "no_p5min_entity"}
    assert imp == [0.1] * 3 and exp == [0.05] * 3


def test_a_newer_run_withdrawing_the_spike_lowers_the_price(monkeypatch):
    """The household's 'if the spike fizzles, stop': every solve reads the
    latest run, so there is nothing to un-latch."""
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: "sensor.p5")
    monkeypatch.setattr(pp, "_offset", lambda retail, wholesale, fn: {0: 0.0})
    grid = _grid(4)

    def solve_with(prices, run):
        monkeypatch.setattr(
            pp.solver_shared, "ha_get", lambda _e: _state(_rows(prices), run)
        )
        exp = [0.05] * 4
        pp.apply(
            _cfg(), grid, grid[1], run, ([0.1] * 4, [""] * 4), (exp, [""] * 4), None
        )
        return exp

    assert max(solve_with([0.08, 0.08, 0.85, 0.85], T0)) == 0.85
    later = T0 + timedelta(minutes=5)
    assert max(solve_with([0.08, 0.08, 0.09, 0.09], later)) == 0.09


def test_a_failure_inside_never_reaches_the_solve(monkeypatch):
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: "sensor.p5")
    monkeypatch.setattr(pp.solver_shared, "ha_get", lambda _e: _state(_rows([1.0])))

    def boom(*_a):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(pp, "_offset", boom)
    imp = [0.1]
    out = pp.apply(_cfg(), _grid(1), T0, T0, (imp, [""]), ([0.0], [""]), None)
    assert out["reason"] == "error" and imp == [0.1]


# ---- the P2P premium --------------------------------------------------------


def test_the_p2p_premium_stays_a_premium_over_the_new_spot():
    # p2p rate 0.20: before = spot 0.05 + bonus 0.15
    before, after = [0.05, 0.05, 0.05], [0.05, 0.12, 0.85]
    assert pp.rebase_export_bonus([0.15, 0.15, 0.15], before, after) == [
        0.15,
        0.20 - 0.12,
        0.0,
    ]


def test_no_bonus_stays_no_bonus():
    assert pp.rebase_export_bonus([0.0, 0.0], [0.05, 0.05], [0.9, 0.9]) == [0.0, 0.0]


# ---- the LP decides ---------------------------------------------------------


def _charge_kwh_before_the_spike(export_prices):
    n = len(export_prices)
    plan = build_plan(
        periods=PeriodGrid(hours=np.full(n, 5 / 60), start=None),
        grid=GridConfig(
            import_price=np.full(n, 0.10),
            export_price=np.asarray(export_prices, dtype=float),
            import_limit_kw=60.0,
            export_limit_kw=60.0,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=30.0,
                initial_soc_kwh=2.0,
                min_soc_kwh=2.0,
                max_soc_kwh=30.0,
                max_charge_kw=15.0,
                max_discharge_kw=15.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.05,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="load", forecast_kw=np.full(n, 1.0))],
    )
    charge = np.asarray(plan.batteries[0].charge_kw)
    return float(np.sum(charge[:6]) * 5 / 60)


def test_a_visible_spike_makes_the_lp_charge_ahead_of_it():
    assert _charge_kwh_before_the_spike([0.05] * 6 + [0.85] * 6) > 1.0


def test_with_the_spike_withdrawn_the_lp_does_not_charge():
    assert _charge_kwh_before_the_spike([0.05] * 12) < 1e-6


def test_the_markup_is_learned_against_the_regional_price_else_p5min_itself(
    monkeypatch,
):
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: "sensor.p5")
    monkeypatch.setattr(pp.solver_shared, "ha_get", lambda _e: _state(_rows([0.9])))
    monkeypatch.setattr(pp, "_offset_cache", {})
    asked = []

    def offset_for(retail, wholesale):
        asked.append((retail, wholesale))
        return {0: 0.0}

    sides = (([0.1], [""]), ([0.0], [""]))
    pp.apply(_cfg(), _grid(1), T0, T0, *sides, offset_for)
    regional = {**_cfg(), "solver_regional_spot_current_price_sensor": "sensor.qld1"}
    pp.apply(regional, _grid(1), T0, T0, *sides, offset_for)
    assert asked == [
        ("sensor.import", "sensor.p5"),
        ("sensor.export", "sensor.p5"),
        ("sensor.import", "sensor.qld1"),
        ("sensor.export", "sensor.qld1"),
    ]
