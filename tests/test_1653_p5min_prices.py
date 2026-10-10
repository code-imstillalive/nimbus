"""nimbus #1653: AEMO's 5-minute pre-dispatch priced into the solve.

No precharge rule exists to test: the overlay changes prices and nothing
else, and two tests show the LP itself charging ahead of a spike it can see
and not charging once the spike is withdrawn.
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
# Wholesale passed straight through: slope 1, no margin.
PASS = pp.Markup(slope=1.0, intercept={0: 0.0}, n_pairs=0)


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


def test_future_periods_take_this_installs_retail_price_for_the_wholesale():
    grid = _grid(4)
    values, sources = [0.10] * 4, ["primary"] * 4
    # 04:30-04:59 is half-hour 9; LocalVolts' buy side, as measured.
    markup = pp.Markup(slope=1.1666, intercept={9: 0.075}, n_pairs=288)
    changed = pp.overlay(
        values, sources, _rows([0.08, 0.70, 0.86, 0.09]), grid, grid[1], markup
    )
    assert changed == [1, 2, 3]
    assert values[0] == 0.10
    assert [round(v, 4) for v in values[1:]] == [0.8916, 1.0783, 0.18]
    assert sources == ["primary", "p5min", "p5min", "p5min"]


def test_the_settled_block_is_never_touched():
    grid = _grid(3)
    values, sources = [0.10] * 3, ["primary"] * 3
    pp.overlay(values, sources, _rows([0.9, 0.9, 0.9]), grid, grid[1], PASS)
    assert values[0] == 0.10 and sources[0] == "primary"


def test_periods_beyond_the_p5min_horizon_keep_their_price():
    grid = _grid(6)
    values, sources = [0.10] * 6, ["primary"] * 6
    pp.overlay(values, sources, _rows([0.5, 0.5]), grid, grid[0], PASS)
    assert values[2:] == [0.10] * 4


def test_a_one_minute_period_takes_the_interval_it_starts_in():
    grid = [T0 + timedelta(minutes=m) for m in (6, 7, 11)]
    values, sources = [0.0] * 3, [""] * 3
    pp.overlay(values, sources, _rows([0.1, 0.6, 0.7]), grid, T0, PASS)
    assert values == [0.6, 0.6, 0.7]


def test_no_learned_markup_means_no_change():
    grid = _grid(2)
    values, sources = [0.10, 0.10], ["primary"] * 2
    assert pp.overlay(values, sources, _rows([0.9, 0.9]), grid, T0, None) == []
    assert values == [0.10, 0.10]


def test_the_real_aemo_file_overlays_cleanly():
    run = p5min.parse_region_solution(FIXTURE.read_text(), "QLD1")
    start = datetime.fromisoformat(run["forecast"][1]["start"])
    grid = _grid(11, start)
    values, sources = [0.0] * 11, [""] * 11
    changed = pp.overlay(values, sources, run["forecast"], grid, start, PASS)
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
    pp.overlay(values, sources, rows, grid, grid[1], PASS)
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
    monkeypatch.setattr(pp, "_markup", lambda retail, wholesale, fn: PASS)
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

    monkeypatch.setattr(pp, "_markup", boom)
    imp = [0.1]
    out = pp.apply(_cfg(), _grid(1), T0, T0, (imp, [""]), ([0.0], [""]), None)
    assert out["reason"] == "error" and imp == [0.1]


def test_the_markup_is_learned_against_the_regional_price_else_p5min_itself(
    monkeypatch,
):
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: "sensor.p5")
    monkeypatch.setattr(pp.solver_shared, "ha_get", lambda _e: _state(_rows([0.9])))
    monkeypatch.setattr(pp, "_markup_cache", {})
    asked = []

    def history_for(entity):
        asked.append(entity)
        return []

    sides = (([0.1], [""]), ([0.0], [""]))
    pp.apply(_cfg(), _grid(1), T0, T0, *sides, history_for)
    regional = {**_cfg(), "solver_regional_spot_current_price_sensor": "sensor.qld1"}
    pp.apply(regional, _grid(1), T0, T0, *sides, history_for)
    assert asked == [
        "sensor.import",
        "sensor.p5",
        "sensor.export",
        "sensor.p5",
        "sensor.import",
        "sensor.qld1",
        "sensor.export",
        "sensor.qld1",
    ]


# ---- learning wholesale -> retail -------------------------------------------
#
# Synthetic histories shaped like the reference install's real feeds,
# measured 10 Oct 2026: LocalVolts posts a provisional price ~3 s into an
# interval and the real one ~15-50 s in; AEMO's current-price sensor ~78 s in.

DAY0 = datetime(2026, 10, 5, tzinfo=AEST)


def _wholesale(days):
    """Wholesale with real movement inside every half-hour, and a spike."""
    out = []
    for k in range(days * 288):
        t = DAY0 + timedelta(minutes=5 * k)
        spike = 0.6 if k % 97 == 0 else 0.0
        out.append((t, 0.05 + 0.03 * ((k * 7) % 11) / 10 + spike))
    return out


def _network(t, evening_peak=True):
    if evening_peak and 16 <= t.hour < 21:
        return 0.2231
    return 0.013 if 11 <= t.hour < 16 else 0.075


def _feeds(days, slope, network=_network, provisional=0.182):
    retail, wholesale = [], []
    for t, x in _wholesale(days):
        retail.append((t + timedelta(seconds=3), provisional))
        retail.append((t + timedelta(seconds=20), slope * x + network(t)))
        wholesale.append((t + timedelta(seconds=78), x))
    return retail, wholesale


def test_settled_takes_the_last_post_of_each_interval():
    t = DAY0 + timedelta(hours=5, minutes=5)
    got = pp.settled_by_interval(
        [(t + timedelta(seconds=3), 0.182), (t + timedelta(seconds=50), 0.912)]
    )
    assert list(got.values()) == [(t, 0.912)]


def test_a_retailer_that_scales_with_wholesale_is_learned_exactly():
    m = pp.fit_markup(*_feeds(5, 1.1666))
    assert abs(m.slope - 1.1666) < 1e-6
    spike = DAY0 + timedelta(days=4, hours=5, minutes=5)
    assert abs(m.retail(0.8601, spike) - 1.0783) < 1e-4
    evening = DAY0 + timedelta(days=4, hours=18)
    assert abs(m.retail(0.10, evening) - (0.11666 + 0.2231)) < 1e-6


def test_a_pure_margin_retailer_fits_slope_one():
    m = pp.fit_markup(*_feeds(3, 1.0, network=lambda t: 0.12))
    assert abs(m.slope - 1.0) < 1e-6
    assert abs(m.retail(0.9, DAY0 + timedelta(days=2, hours=3)) - 1.02) < 1e-6


def test_pairing_on_the_interval_is_exact():
    """The bug this replaces: retail posted at :20 paired with the wholesale
    posted at :78 of the PREVIOUS interval, plus the provisional post.
    Pairing settled values on the interval recovers the relation exactly."""
    m = pp.fit_markup(*_feeds(2, 1.0605, network=lambda t: 0.0))
    assert abs(m.slope - 1.0605) < 1e-6
    assert max(abs(v) for v in m.intercept.values()) < 1e-9


def test_a_tariff_change_between_days_moves_the_margin_not_the_slope():
    """LocalVolts moved this install from TOU to flat 7.5 c network on 9 Oct
    2026. A week's average would blend both; the fit follows the new one."""
    cut = DAY0 + timedelta(days=4)

    def network(t):
        return _network(t, evening_peak=t < cut)

    m = pp.fit_markup(*_feeds(5, 1.1666, network=network))
    assert abs(m.slope - 1.1666) < 1e-6
    assert abs(m.intercept[36] - 0.075) < 1e-9  # 18:00, now flat
    assert abs(m.intercept[24] - 0.013) < 1e-9  # 12:00, unchanged


def test_one_glitched_interval_does_not_tilt_the_slope():
    retail, wholesale = _feeds(3, 1.0605, network=lambda t: 0.0)
    t = DAY0 + timedelta(days=1, hours=10, minutes=5)
    retail.append((t + timedelta(seconds=40), 5.0))
    m = pp.fit_markup(retail, wholesale)
    assert abs(m.slope - 1.0605) < 1e-4


def test_under_a_day_of_pairs_learns_nothing():
    retail, wholesale = _feeds(1, 1.0605)
    assert pp.fit_markup(retail[:-4], wholesale[:-2]) is None


def test_no_wholesale_movement_falls_back_to_a_pure_margin():
    flat = [(DAY0 + timedelta(minutes=5 * k, seconds=78), 0.1) for k in range(400)]
    retail = [(DAY0 + timedelta(minutes=5 * k, seconds=20), 0.2) for k in range(400)]
    m = pp.fit_markup(retail, flat)
    assert m.slope == 1.0
    assert abs(m.retail(0.5, DAY0 + timedelta(hours=1)) - 0.6) < 1e-9


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


def test_why_it_is_inactive_is_logged_once_per_change(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(pp, "_last_reason", None)
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: "sensor.p5")
    stale = _state(_rows([0.9]), T0 - timedelta(minutes=30))
    monkeypatch.setattr(pp.solver_shared, "ha_get", lambda _e: stale)
    sides = (([0.1], [""]), ([0.0], [""]))
    with caplog.at_level(logging.INFO, logger=pp.__name__):
        for _ in range(3):
            pp.apply(_cfg(), _grid(1), T0, T0, *sides, None)
        monkeypatch.setattr(pp, "resolve_entity_id", lambda: None)
        pp.apply(_cfg(), _grid(1), T0, T0, *sides, None)
    msgs = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert msgs == [
        (
            "WARNING",
            (
                "Nimbus #1653: P5MIN overlay inactive: sensor.p5 has no run from "
                "the last 15 minutes; spikes are not visible"
            ),
        ),
        (
            "INFO",
            (
                "Nimbus #1653: P5MIN overlay inactive: no P5MIN sensor on this "
                "install (non-NEM, or no region found)"
            ),
        ),
    ]


def test_the_priced_line_names_the_highest_prices_it_set(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(pp, "_last_reason", None)
    monkeypatch.setattr(pp, "_last_logged_run", None)
    monkeypatch.setattr(pp, "resolve_entity_id", lambda: "sensor.p5")
    monkeypatch.setattr(pp, "_markup", lambda retail, wholesale, fn: PASS)
    rows = _rows([0.1, 0.25, 0.86])
    monkeypatch.setattr(pp.solver_shared, "ha_get", lambda _e: _state(rows))
    grid = _grid(3)
    sides = (([0.1] * 3, [""] * 3), ([0.05] * 3, [""] * 3))
    with caplog.at_level(logging.INFO, logger=pp.__name__):
        pp.apply(_cfg(), grid, grid[1], T0, *sides, None)
    assert caplog.records[-1].getMessage() == (
        "Nimbus #1653: P5MIN run 2026-10-09T04:30:00+10:00 priced 2 import / "
        "2 export periods (highest import 0.8600, export 0.8600 $/kWh)"
    )
