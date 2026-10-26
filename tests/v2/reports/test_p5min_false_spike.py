"""A P5MIN spike the market itself contradicts is not planned on (#1778)
(#1837).

Rule (a): a run whose first interval violates a constraint and forecasts at
or near the cap is infeasible. Rule (b): dispatch came in far below the
forecast for the same interval. While either holds, forward spikes are
replaced; a clean run releases. The threshold is 5,000 $/MWh or 90 % of the
cap, and 0 turns the guard off. The fixtures are public AEMO NEMWEB files
from 15 Oct 2026 (market data, not household data).
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import number as number_mod
from custom_components.nimbus_load import p5min, p5min_guard
from custom_components.nimbus_load import sensor_p5min as sp

NEM = p5min.NEM_TIME
DIR = Path(__file__).resolve().parents[2] / "fixtures" / "p5min" / "2026-10-15"
OCT9 = sorted((DIR.parent / "2026-10-09").glob("*.CSV"))
RUNS = sorted(DIR.glob("PUBLIC_P5MIN_*.CSV"))
DISPATCH = sorted(DIR.glob("PUBLIC_DISPATCHIS_*.CSV"))
THR = p5min_guard.threshold_kwh(5000.0)  # 5.0 $/kWh


def _run(stamp: str) -> tuple[dict, datetime]:
    path = next(p for p in RUNS if p.stem[13:25] == stamp)
    run = p5min.parse_run(path.read_text(), "QLD1")
    published = p5min.published_at(path.stem + ".zip")
    assert run is not None and published is not None
    return run, published


def _dispatch(path: Path) -> tuple[datetime, float]:
    """(interval end, QLD1 RRP in $/kWh) from a trimmed DISPATCHIS file."""
    header: list[str] = []
    for rec in csv.reader(io.StringIO(path.read_text())):
        if rec[:3] == ["I", "DISPATCH", "PRICE"]:
            header = rec
        elif rec[:3] == ["D", "DISPATCH", "PRICE"]:
            r = dict(zip(header, rec, strict=True))
            if r["REGIONID"] == "QLD1" and r["INTERVENTION"] == "0":
                end = datetime.strptime(
                    r["SETTLEMENTDATE"], "%Y/%m/%d %H:%M:%S"
                ).replace(tzinfo=NEM)
                return end, float(r["RRP"]) / 1000.0
    raise AssertionError(f"no QLD1 price in {path.name}")


def _no_constraints(run: dict) -> dict:
    return {**run, "violated": []}


def _t(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(2026, 10, 15, h, m, s, tzinfo=NEM)


# ---- the fixture is the event Mark describes -------------------------------


# ---- rule (a): infeasible spike ---------------------------------------------


def test_threshold_is_5000_or_within_10_percent_of_the_cap():
    # test_threshold_is_5000_or_within_10_percent_of_the_cap
    # test_threshold_is_5000_or_within_10_percent_of_the_cap: Rule 2(a) threshold: >= $5,000/MWh, or 90% of the $23,200 cap when the
    assert p5min_guard.threshold_kwh(5000.0) == 5.0
    assert p5min_guard.threshold_kwh(22000.0) == 20.88
    assert p5min_guard.threshold_kwh(None) == 5.0
    assert p5min_guard.threshold_kwh(float("nan")) == 5.0
    assert p5min_guard.threshold_kwh(0.0) == float("inf")
    assert p5min_guard.threshold_kwh(-1.0) == float("inf")

    # test_the_setting_can_be_set_to_zero
    desc = next(
        d
        for d in number_mod._DESCRIPTIONS
        if d.key == "solver_p5min_false_spike_threshold_mwh"
    )
    assert desc.min_value == 0

    # test_rule_b_flags_the_1615_pair: Rule (b): Mark's minimal pair. P5MIN run 16:15 forecast $23,200 for
    run, published = _run("202610151615")
    end, price = _dispatch(next(p for p in DISPATCH if "202610151615" in p.name))
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_no_constraints(run), published)
    assert not g.active
    g.on_dispatch(end, price, _t("16:10:45"))
    assert g.active and g.reason == p5min_guard.REASON_DISPATCH
    assert g.detail == {
        "interval_end": "2026-10-15T16:15:00+10:00",
        "forecast": 23.2,
        "dispatch": 0.10255,
    }
    assert g.compared[-1] == {
        "end": "2026-10-15T16:15:00+10:00",
        "forecast": 23.2,
        "dispatch": 0.10255,
    }

    # test_dispatch_never_spiked_on_15_oct: The other half of the evidence: QLD1 dispatch stayed $44-$295/MWh.
    prices_kwh = [_dispatch(p)[1] for p in DISPATCH]
    assert max(prices_kwh) == 0.295 and min(prices_kwh) > 0.04

    # test_rule_c_needs_a_normal_dispatch_price: No dispatch price seen yet, or a dispatch spike: rule (c) does not fire.
    run, published = _run("202610151550")
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_calm(run), published)
    assert not g.active
    g.on_dispatch(_t("15:50:00"), 1.5, _t("15:45:05"))
    g.on_run(_calm(run), published)
    assert not g.active

    # test_rule_c_renews_and_a_clean_run_releases: Each run that still violates the CA_ constraint renews the flag; the
    g = p5min_guard.FalseSpikeGuard()
    g.on_dispatch(_t("15:50:00"), 0.06, _t("15:45:05"))
    run, published = _run("202610151550")
    g.on_run(_calm(run), published)
    run2, published2 = _run("202610151555")
    g.on_run(_calm(run2), published2)
    assert g.renewed == published2
    g.on_run(*_run("202610151635"))
    assert not g.active
    assert g.last_release["reason"] == p5min_guard.RELEASE_CLEAN

    # test_release_on_a_real_dispatch_spike: Release rule: a dispatch interval at or above the threshold releases
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    assert g.active
    g.on_dispatch(_t("15:55:00"), 15.0, _t("15:50:05"))
    assert not g.active and g.latched
    assert g.last_release["reason"] == p5min_guard.RELEASE_DISPATCH_SPIKE
    g.on_run(*_run("202610151555"))
    assert not g.active

    # test_a_spike_dispatch_confirms_is_not_suppressed: A genuine spike: P5MIN forecasts the cap with no violated constraint,
    run, published = _run("202610151615")
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_no_constraints(run), published)
    g.on_dispatch(_t("16:15:00"), 23.2, _t("16:10:05"))
    assert not g.active and g.latched
    grid = _grid(_t("16:10:00"), 6)
    imp, exp = [0.42] + [26.31] * 5, [0.31] + [23.86] * 5
    out = p5min_guard.guard_schedule_prices(imp, exp, grid, 5000.0, _ha({P5: _flag(g)}))
    assert out[0] is imp and out[1] is exp

    # test_the_9_oct_spike_is_not_suppressed
    # test_the_9_oct_spike_is_not_suppressed: 9 Oct's real spike peaked at ~$500/MWh in P5MIN, below the threshold,
    s = _sensor()
    fired = []
    for path in OCT9:
        s.apply(path.stem + ".zip", p5min.parse_run(path.read_text(), "QLD1"))
        a = s.extra_state_attributes
        assert a["false_spike_guard"]["active"] is False
        fired.append((a["run_datetime"][11:16], a["readiness_signal"]))
    assert next(run for run, on in fired if on) == "04:30"

    # test_import_and_export_forward_spikes_are_replaced: Act rule, both sides: a high import price and a high POSITIVE feed-in
    grid = _grid(_t("16:10:00"), 6)
    imp = [0.42, 26.31, 26.31, 0.30, 26.31, 26.31]
    exp = [0.31, 23.86, 23.86, 0.20, 23.86, 23.86]
    out_imp, out_exp = p5min_guard.guard_schedule_prices(
        imp, exp, grid, 5000.0, _ha({P5: _active_flag()})
    )
    assert out_imp == [0.42, 0.42, 0.42, 0.30, 0.42, 0.42]
    assert out_exp == [0.31, 0.31, 0.31, 0.20, 0.31, 0.31]
    assert imp[1] == 26.31  # the inputs are not mutated

    # test_a_localvolts_forward_spike_is_replaced_while_the_flag_holds: Act rule, LocalVolts included (Mark: LocalVolts carries incorrect
    grid = _grid(_t("16:10:00"), 4)
    imp, exp = _localvolts(grid, [0.42, 27.06, 27.06, 0.40], [0.31, 24.60, 24.60, 0.29])
    flag = _ha({P5: _active_flag()})
    out_imp, out_exp = p5min_guard.guard_schedule_prices(imp, exp, grid, 5000.0, flag)
    assert out_imp == [0.42, 0.42, 0.42, 0.40]
    assert out_exp == [0.31, 0.31, 0.31, 0.29]
    no_flag = _ha({P5: {"state": "0.1", "attributes": {}}})
    kept = p5min_guard.guard_schedule_prices(imp, exp, grid, 5000.0, no_flag)
    assert kept == (imp, exp)
    late = _grid(_t("16:50:00"), 4)  # raised 15:46:27, expires 16:46:27
    kept = p5min_guard.guard_schedule_prices(imp, exp, late, 5000.0, flag)
    assert kept == (imp, exp)
    off = MagicMock(side_effect=AssertionError("the flag is not read when off"))
    assert p5min_guard.guard_schedule_prices(imp, exp, grid, 0.0, off) == (imp, exp)

    # test_without_a_spike_the_flag_is_not_even_read: A solve with no forward spike reads nothing extra (the golden master
    grid = _grid(_t("16:10:00"), 3)
    calm = [0.42, 0.40, 0.38]
    read = MagicMock(side_effect=AssertionError("read"))
    kept = p5min_guard.guard_schedule_prices(calm, calm, grid, 5000.0, read)
    assert kept == (calm, calm)
    spiky = [0.42, 26.31, 0.38]
    missing = MagicMock(side_effect=KeyError(P5))
    kept = p5min_guard.guard_schedule_prices(spiky, calm, grid, 5000.0, missing)
    assert kept == (spiky, calm)

    # test_the_current_interval_is_never_replaced: Act rule: every period inside the current 5-minute interval keeps its
    values = [26.31] * 8
    grid = _grid(_t("16:12:00"), 8, step=1)
    out, replaced, fallback = p5min_guard.replace_spikes(values, grid, THR, 0.42)
    # 16:12, 16:13, 16:14 are inside 16:10-16:15.
    assert out == [26.31] * 3 + [0.42] * 5 and replaced == 5 and fallback == 0.42
    on_boundary = _grid(_t("16:15:00"), 3)
    out, _, _ = p5min_guard.replace_spikes([26.31] * 3, on_boundary, THR, 0.42)
    assert out == [26.31, 0.42, 0.42]

    # test_fallback_when_the_current_price_is_a_spike: Act rule: a spiking current price is not used; the current interval's
    grid = _grid(_t("16:10:00"), 4)
    out, _, fb = p5min_guard.replace_spikes([0.2, 26.0, 0.4, 26.0], grid, THR, 23.2)
    assert out == [0.2, 0.2, 0.4, 0.2] and fb == 0.2
    out, _, fb = p5min_guard.replace_spikes([26.0, 26.0, 0.4, 26.0], grid, THR, None)
    assert out == [26.0, 0.4, 0.4, 0.4] and fb == 0.4
    out, n, fb = p5min_guard.replace_spikes([26.0] * 4, grid, THR, 23.2)
    assert out == [26.0] * 4 and n == 0 and fb is None

    # test_main_guards_the_scheduling_prices_after_the_blend: Wiring: main() applies the guard to spot_import_raw/spot_export after
    import inspect

    src = inspect.getsource(solver_writer.main)
    guard = src.index("p5min_guard.guard_schedule_prices(")
    assert src.index("blend_price_with_secondary_sources(") < guard
    assert src.index('import_price_source[_i] = "primary"') < guard
    assert '"solver_p5min_false_spike_threshold_mwh"' in src[guard : guard + 400]
    assert "spot_import_raw, spot_export = p5min_guard" in src

    # test_the_threshold_is_a_dashboard_number_in_aud_per_mwh
    desc = next(
        d
        for d in number_mod._DESCRIPTIONS
        if d.key == "solver_p5min_false_spike_threshold_mwh"
    )
    assert desc.default == 5000.0 and desc.unit == "AUD/MWh"
    assert desc.min_value <= 5000.0 <= desc.max_value == 23200


# ---- rule (b): dispatch disagrees -------------------------------------------


# ---- the whole event, replayed ---------------------------------------------


def _replay():
    """Runs and dispatch in publication order: dispatch for T (~T-4:56),
    then P5MIN run T (~T-4:20)."""
    g = p5min_guard.FalseSpikeGuard()
    timeline = []
    for run_path, disp_path in zip(RUNS, DISPATCH, strict=True):
        end, price = _dispatch(disp_path)
        g.on_dispatch(end, price, end - timedelta(minutes=4, seconds=56))
        run, published = _run(run_path.stem[13:25])
        g.on_run(run, published)
        timeline.append((run["run_datetime"][11:16], g.active, g.reason))
    return g, timeline


# ---- release -----------------------------------------------------------------


def _calm(run: dict) -> dict:
    """The run with every forecast price at a normal 9c/kWh: the shape of
    16 Oct 2026, when P5MIN violated CA_BRIS_59E3F2B6_1 by 560-980 MW at
    normal prices while the 30-minute pre-dispatch carried the spike."""
    return {**run, "forecast": [{**r, "value": 0.09} for r in run["forecast"]]}


# ---- the solver side: Nimbus's scheduling prices ----------------------------
# Mark Purcell on #1779: "This isn't an Amber vs Localvolts issue. Both carry
# incorrect spike forecasts... This guard should be against the Nimbus
# scheduling prices." So it acts on the final import/export arrays the plan is
# solved against, whatever the source.

P5 = p5min_guard.P5MIN_ENTITY_ID


def _grid(start: datetime, n: int, step: int = 5) -> list[datetime]:
    return [start + timedelta(minutes=step * i) for i in range(n)]


def _flag(g: p5min_guard.FalseSpikeGuard) -> dict:
    return {"state": "23.2", "attributes": {p5min_guard.ATTRIBUTE: g.attribute()}}


def _active_flag() -> dict:
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    return _flag(g)


def _ha(states: dict):
    return MagicMock(side_effect=lambda eid: states[eid])


def _localvolts(grid, buy, sell):
    """A LocalVolts v2 Flex Up forecast, read through the solver's own
    LocalVolts reader into scheduling-price arrays."""
    rows = [
        {"time": t.isoformat(), "costsflexup": b, "earningsflexup": e}
        for t, b, e in zip(grid, buy, sell)
    ]
    imp, _ = solver_writer.resample_price_with_extrapolation(
        rows, "costsflexup", grid, [], {}
    )
    exp, _ = solver_writer.resample_price_with_extrapolation(
        rows, "earningsflexup", grid, [], {}
    )
    return imp, exp


# ---- the sensor ----------------------------------------------------------------


def _sensor(dispatch_sensor: str | None = None):
    entry = MagicMock()
    entry.entry_id = "e1778"
    entry.data = (
        {sp.CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR: dispatch_sensor}
        if dispatch_sensor
        else {}
    )
    entry.options = {}
    s = sp.NimbusP5MinForecastSensor(entry, "QLD1", "0.94.457")
    s.hass = None
    return s


def _apply(s, stamp: str) -> dict:
    path = next(p for p in RUNS if p.stem[13:25] == stamp)
    s.apply(path.stem + ".zip", p5min.parse_run(path.read_text(), "QLD1"))
    return s.extra_state_attributes
