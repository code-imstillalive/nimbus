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
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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


def test_fixture_matches_marks_table():
    """#1778's table: the 16:15 run's CA_BRIS row, RHS 1,446 MW, violated by
    1,117 MW, total generic violation ~9,300, every interval at the cap; and
    dispatch's 16:15 price, $102.55/MWh."""
    assert len(RUNS) == 14 and len(DISPATCH) == 14
    run, published = _run("202610151615")
    assert published == _t("16:10:40")
    assert run["case"] == {
        "total_generic_violation": 9307.149,
        "total_interconnector_violation": 0.0,
    }
    assert {r["value"] for r in run["forecast"]} == {23.2}
    first = p5min.first_interval_violations(run)
    assert [v["constraint_id"] for v in first] == ["CA_BRIS_59E2F7DC_1"]
    assert first[0]["rhs"] == 1445.60749 and first[0]["violation"] == 1117.119
    assert first[0]["marginal_value"] == -464000.0
    end, price = _dispatch(next(p for p in DISPATCH if "202610151615" in p.name))
    assert end == _t("16:15:00") and price == 0.10255


def test_parse_keeps_only_violated_constraint_rows():
    """Rule 1 (parse): CONSTRAINTSOLUTION rows with VIOLATIONDEGREE > 0 only.
    The fixture carries three non-violated rows per run on purpose."""
    run, _ = _run("202610151615")
    assert len(run["violated"]) == 12
    assert all(v["violation"] > 0 for v in run["violated"])
    assert "F_I+NIL_MG_R1" in RUNS[6].read_text()  # present in the file...
    assert all(v["constraint_id"] != "F_I+NIL_MG_R1" for v in run["violated"])
    assert p5min.max_violation(run)["interval_end"] == "2026-10-15T16:15:00+10:00"


def test_a_nonzero_total_alone_is_not_infeasibility():
    """15:45 (and 16:35) carry a generic-violation total near 1,000-2,000
    with no violated row and normal prices. The totals are published, not
    used as the gate."""
    run, _ = _run("202610151545")
    assert run["case"]["total_generic_violation"] == 957.759
    assert run["violated"] == []
    assert p5min_guard.infeasible_spike(run, THR) is None


# ---- rule (a): infeasible spike ---------------------------------------------


def test_rule_a_needs_a_violated_constraint():
    """Rule (a): the same spike with no violated constraint is not flagged."""
    run, published = _run("202610151550")
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_no_constraints(run), published)
    assert not g.active


def test_rule_a_prefers_a_ca_constraint_but_does_not_require_one():
    """Rule (a): a non-CA_ violated constraint still flags."""
    run, published = _run("202610151550")
    renamed = {
        **run,
        "violated": [{**v, "constraint_id": "Q_X"} for v in run["violated"]],
    }
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(renamed, published)
    assert g.active and g.detail["constraint_id"] == "Q_X"


def test_threshold_is_5000_or_within_10_percent_of_the_cap():
    """Rule 2(a) threshold: >= $5,000/MWh, or 90% of the $23,200 cap when the
    setting is higher; unset is the default; 0 or below is off."""
    assert p5min_guard.threshold_kwh(5000.0) == 5.0
    assert p5min_guard.threshold_kwh(22000.0) == 20.88
    assert p5min_guard.threshold_kwh(None) == 5.0
    assert p5min_guard.threshold_kwh(float("nan")) == 5.0
    assert p5min_guard.threshold_kwh(0.0) == float("inf")
    assert p5min_guard.threshold_kwh(-1.0) == float("inf")


def test_zero_turns_the_guard_off_over_the_real_event():
    """Mark's review on #1779: a genuine off switch, "0 = off" like Charge By
    and the reserve. Replayed over 15 Oct with the setting at 0, the guard
    never raises, so no forward price is rewritten."""
    g = p5min_guard.FalseSpikeGuard(threshold_mwh=0.0)
    for run_path, disp_path in zip(RUNS, DISPATCH, strict=True):
        end, price = _dispatch(disp_path)
        g.on_dispatch(end, price, end - timedelta(minutes=4, seconds=56))
        run, published = _run(run_path.stem[13:25])
        g.on_run(run, published)
        assert not g.active, run["run_datetime"]
    assert g.last_release is None


def test_the_setting_can_be_set_to_zero():
    desc = next(
        d
        for d in number_mod._DESCRIPTIONS
        if d.key == "solver_p5min_false_spike_threshold_mwh"
    )
    assert desc.min_value == 0


# ---- rule (b): dispatch disagrees -------------------------------------------


def test_rule_b_flags_the_1615_pair():
    """Rule (b): Mark's minimal pair. P5MIN run 16:15 forecast $23,200 for
    the 16:15 interval; dispatch came in at $102.55. Constraint rows removed
    so only (b) can fire."""
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


def test_rule_b_uses_the_run_before_dispatch():
    """Rule (b): dispatch for 16:15 is published (~16:10:04) before P5MIN's
    16:15 run (16:10:40), so the forecast compared is the 16:10 run's row
    for 16:15 -- the last run before dispatch."""
    run, published = _run("202610151610")
    end, price = _dispatch(next(p for p in DISPATCH if "202610151615" in p.name))
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_no_constraints(run), published)
    g.on_dispatch(end, price, _t("16:10:04"))
    assert g.active and g.detail["forecast"] == 23.2


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


def test_replay_flags_1550_to_1630_and_releases_on_the_clean_1635_run():
    """Rules (a) and release-on-clean-forecast, over the real event: on from
    the 15:50 run, held through 16:30, released by the 16:35 run (published
    16:30:46), 44 minutes later -- inside the 60-minute timeout."""
    g, timeline = _replay()
    on = [run for run, active, _ in timeline if active]
    assert on[0] == "15:50" and on[-1] == "16:30"
    assert on == [r for r, *_ in timeline if "15:50" <= r <= "16:30"]
    assert g.last_release == {
        "reason": p5min_guard.RELEASE_CLEAN,
        "at": "2026-10-15T16:30:46+10:00",
    }
    assert not g.latched


def test_dispatch_never_spiked_on_15_oct():
    """The other half of the evidence: QLD1 dispatch stayed $44-$295/MWh."""
    prices_kwh = [_dispatch(p)[1] for p in DISPATCH]
    assert max(prices_kwh) == 0.295 and min(prices_kwh) > 0.04


# ---- release -----------------------------------------------------------------


def test_release_on_timeout_and_no_rearm_until_clean():
    """Release rule: 60 minutes after it was raised when no run renews it.
    After the timeout a still-infeasible run does not re-arm it; a clean run
    clears the latch."""
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    since = g.since
    g.expire(since + timedelta(minutes=59))
    assert g.active
    g.expire(since + timedelta(minutes=60))
    assert not g.active and g.latched
    assert g.last_release["reason"] == p5min_guard.RELEASE_TIMEOUT
    run, _ = _run("202610151630")
    g.on_run(run, since + timedelta(minutes=61))
    assert not g.active
    g.on_run(*_run("202610151635"))
    assert not g.latched
    g.on_run(*_run("202610151630"))
    assert g.active


def test_a_still_infeasible_run_renews_the_flag():
    """16 Oct 2026: the same false spike sat in the forecasts from 13:00 to
    16:00. Each run that still forecasts the spike AND still violates a
    constraint renews the flag, so the 60 minutes count from the last such
    run, not from the first."""
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    since = g.since
    run, _ = _run("202610151630")
    g.on_run(run, since + timedelta(minutes=50))
    assert g.since == since and g.renewed == since + timedelta(minutes=50)
    g.expire(since + timedelta(minutes=60))
    assert g.active
    attr = g.attribute()
    assert attr["expires_at"] == (since + timedelta(minutes=110)).isoformat()
    assert attr["renewed"] == (since + timedelta(minutes=50)).isoformat()
    g.expire(since + timedelta(minutes=110))
    assert not g.active and g.latched
    assert g.last_release["reason"] == p5min_guard.RELEASE_TIMEOUT
    assert g.attribute()["renewed"] is None


def test_a_spike_run_with_no_violation_does_not_renew():
    """Only rule (a)'s evidence renews: a spike run that no longer violates
    any constraint leaves the 60 minutes running from the last one that did."""
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    since = g.since
    run, _ = _run("202610151630")
    g.on_run(_no_constraints(run), since + timedelta(minutes=50))
    assert g.active and g.renewed is None
    g.expire(since + timedelta(minutes=60))
    assert not g.active


def _calm(run: dict) -> dict:
    """The run with every forecast price at a normal 9c/kWh: the shape of
    16 Oct 2026, when P5MIN violated CA_BRIS_59E3F2B6_1 by 560-980 MW at
    normal prices while the 30-minute pre-dispatch carried the spike."""
    return {**run, "forecast": [{**r, "value": 0.09} for r in run["forecast"]]}


def test_rule_c_a_control_room_violation_at_normal_prices():
    """Rule (c): the run's first interval violates a CA_ constraint, P5MIN
    prices nothing high, and the latest dispatch price is normal."""
    run, published = _run("202610151550")
    g = p5min_guard.FalseSpikeGuard()
    g.on_dispatch(_t("15:50:00"), 0.06, _t("15:45:05"))
    g.on_run(_calm(run), published)
    assert g.active and g.reason == p5min_guard.REASON_CONTROL_ROOM
    assert g.detail["constraint_id"].startswith("CA_BRIS_")


def test_rule_c_needs_a_ca_constraint():
    """A violated constraint that is not a control-room one is not rule (c)."""
    run, published = _run("202610151550")
    other = [
        {**v, "constraint_id": "Q>>NIL_" + v["constraint_id"]} for v in run["violated"]
    ]
    g = p5min_guard.FalseSpikeGuard()
    g.on_dispatch(_t("15:50:00"), 0.06, _t("15:45:05"))
    g.on_run({**_calm(run), "violated": other}, published)
    assert not g.active


def test_rule_c_renews_and_a_clean_run_releases():
    """Each run that still violates the CA_ constraint renews the flag; the
    first run with neither a spike nor a CA_ violation releases it."""
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


def test_rule_c_needs_a_material_violation():
    """16 Oct 11:10: CA_BRIS_59E3F2B6_1 RHS 1991 MW violated by 79 MW at
    normal prices, ratio 0.96 -- not material. 15 Oct's spiked runs sit at
    0.52-0.75."""
    run, published = _run("202610151550")
    first = p5min.first_interval_violations(run)
    small = [{**v, "rhs": 1991.29, "violation": 79.368} for v in first]
    g = p5min_guard.FalseSpikeGuard()
    g.on_dispatch(_t("15:50:00"), 0.06, _t("15:45:05"))
    g.on_run({**_calm(run), "violated": small}, published)
    assert not g.active
    for path in RUNS:
        hit = p5min_guard.control_room_violation(
            p5min.parse_run(path.read_text(), "QLD1")
        )
        if hit is not None:
            assert 0.5 < p5min_guard._ratio(hit) <= 0.76, path.name


def test_no_9_oct_run_violates_a_control_room_constraint():
    """9 Oct was a real spike: no run's first interval violates a CA_
    constraint, so rule (c) cannot suppress it."""
    for path in OCT9:
        if "P5MIN" not in path.name:
            continue
        run = p5min.parse_run(path.read_text(), "QLD1")
        assert p5min_guard.control_room_violation(run) is None, path.name


def test_every_15_oct_spiked_run_violates_a_control_room_constraint():
    for path in RUNS:
        run = p5min.parse_run(path.read_text(), "QLD1")
        spiked = p5min_guard.has_spike(run["forecast"], THR)
        assert (p5min_guard.control_room_violation(run) is not None) == spiked, (
            path.name
        )


def test_release_on_clean_forecast():
    """Release rule: a run with no spike interval left."""
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151630"))
    assert g.active
    g.on_run(*_run("202610151635"))
    assert not g.active
    assert g.last_release["reason"] == p5min_guard.RELEASE_CLEAN


def test_release_on_a_real_dispatch_spike():
    """Release rule: a dispatch interval at or above the threshold releases
    the flag and stops (a) re-arming until a clean run (synthetic dispatch
    price: 15 Oct never spiked)."""
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    assert g.active
    g.on_dispatch(_t("15:55:00"), 15.0, _t("15:50:05"))
    assert not g.active and g.latched
    assert g.last_release["reason"] == p5min_guard.RELEASE_DISPATCH_SPIKE
    g.on_run(*_run("202610151555"))
    assert not g.active


def test_a_spike_dispatch_confirms_is_not_suppressed():
    """A genuine spike: P5MIN forecasts the cap with no violated constraint,
    and dispatch prints the cap. Neither rule fires, and the scheduling
    prices pass through untouched -- the same objects. Synthetic (the 16:15
    run with its constraint rows removed, and a $23,200 dispatch price): no
    fixture holds a dispatch-confirmed spike."""
    run, published = _run("202610151615")
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_no_constraints(run), published)
    g.on_dispatch(_t("16:15:00"), 23.2, _t("16:10:05"))
    assert not g.active and g.latched
    grid = _grid(_t("16:10:00"), 6)
    imp, exp = [0.42] + [26.31] * 5, [0.31] + [23.86] * 5
    out = p5min_guard.guard_schedule_prices(imp, exp, grid, 5000.0, _ha({P5: _flag(g)}))
    assert out[0] is imp and out[1] is exp


def test_the_9_oct_spike_is_not_suppressed():
    """9 Oct's real spike peaked at ~$500/MWh in P5MIN, below the threshold,
    so the guard stays off for every run and #1660's readiness signal still
    fires at 04:30 (that fixture carries no constraint rows, so this says
    nothing about rule (a) on 9 Oct)."""
    s = _sensor()
    fired = []
    for path in OCT9:
        s.apply(path.stem + ".zip", p5min.parse_run(path.read_text(), "QLD1"))
        a = s.extra_state_attributes
        assert a["false_spike_guard"]["active"] is False
        fired.append((a["run_datetime"][11:16], a["readiness_signal"]))
    assert next(run for run, on in fired if on) == "04:30"


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


def test_import_and_export_forward_spikes_are_replaced():
    """Act rule, both sides: a high import price and a high POSITIVE feed-in
    are spikes alike (the site's $/kWh, against the converted threshold).
    Forward spikes take the current interval's price; the current interval
    keeps its own. The flag was raised at 15:46:27, so it holds at 16:10."""
    grid = _grid(_t("16:10:00"), 6)
    imp = [0.42, 26.31, 26.31, 0.30, 26.31, 26.31]
    exp = [0.31, 23.86, 23.86, 0.20, 23.86, 23.86]
    out_imp, out_exp = p5min_guard.guard_schedule_prices(
        imp, exp, grid, 5000.0, _ha({P5: _active_flag()})
    )
    assert out_imp == [0.42, 0.42, 0.42, 0.30, 0.42, 0.42]
    assert out_exp == [0.31, 0.31, 0.31, 0.20, 0.31, 0.31]
    assert imp[1] == 26.31  # the inputs are not mutated


def test_the_current_interval_is_never_replaced():
    """Act rule: every period inside the current 5-minute interval keeps its
    real price, on 1-minute periods and from a mid-interval start."""
    values = [26.31] * 8
    grid = _grid(_t("16:12:00"), 8, step=1)
    out, replaced, fallback = p5min_guard.replace_spikes(values, grid, THR, 0.42)
    # 16:12, 16:13, 16:14 are inside 16:10-16:15.
    assert out == [26.31] * 3 + [0.42] * 5 and replaced == 5 and fallback == 0.42
    on_boundary = _grid(_t("16:15:00"), 3)
    out, _, _ = p5min_guard.replace_spikes([26.31] * 3, on_boundary, THR, 0.42)
    assert out == [26.31, 0.42, 0.42]


def test_fallback_when_the_current_price_is_a_spike():
    """Act rule: a spiking current price is not used; the current interval's
    calm price is, else the series' median calm price, else nothing."""
    grid = _grid(_t("16:10:00"), 4)
    out, _, fb = p5min_guard.replace_spikes([0.2, 26.0, 0.4, 26.0], grid, THR, 23.2)
    assert out == [0.2, 0.2, 0.4, 0.2] and fb == 0.2
    out, _, fb = p5min_guard.replace_spikes([26.0, 26.0, 0.4, 26.0], grid, THR, None)
    assert out == [26.0, 0.4, 0.4, 0.4] and fb == 0.4
    out, n, fb = p5min_guard.replace_spikes([26.0] * 4, grid, THR, 23.2)
    assert out == [26.0] * 4 and n == 0 and fb is None


def test_main_guards_the_scheduling_prices_after_the_blend():
    """Wiring: main() applies the guard to spot_import_raw/spot_export after
    the blend and the settled-block re-assert, before fees, with the
    dashboard threshold."""
    import inspect

    src = inspect.getsource(solver_writer.main)
    guard = src.index("p5min_guard.guard_schedule_prices(")
    assert src.index("blend_price_with_secondary_sources(") < guard
    assert src.index('import_price_source[_i] = "primary"') < guard
    assert '"solver_p5min_false_spike_threshold_mwh"' in src[guard : guard + 400]
    assert "spot_import_raw, spot_export = p5min_guard" in src


def test_the_threshold_is_a_dashboard_number_in_aud_per_mwh():
    desc = next(
        d
        for d in number_mod._DESCRIPTIONS
        if d.key == "solver_p5min_false_spike_threshold_mwh"
    )
    assert desc.default == 5000.0 and desc.unit == "AUD/MWh"
    assert desc.min_value <= 5000.0 <= desc.max_value == 23200


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


def test_sensor_publishes_the_constraint_evidence():
    """Rule 1 (expose): the first interval's violated constraints, the run's
    max violation and the totals; the growing lists are unrecorded."""
    a = _apply(_sensor(), "202610151615")
    assert a["total_generic_violation"] == 9307.149
    assert a["total_interconnector_violation"] == 0.0
    assert [v["constraint_id"] for v in a["violated_constraints"]] == [
        "CA_BRIS_59E2F7DC_1"
    ]
    assert a["max_violation"]["violation"] == 1117.119
    assert {"violated_constraints", "forecast_at_dispatch"} <= (
        sp.NimbusP5MinForecastSensor._unrecorded_attributes
    )


def test_readiness_signal_is_suppressed_while_the_guard_holds():
    """Rule 6: #1660's shadow readiness signal would have confirmed on the
    15:50 + 15:55 pair (it did, live); with the guard holding it stays off,
    and says why."""
    run_1550, _ = _run("202610151550")
    run_1555, _ = _run("202610151555")
    assert p5min.spike_confirmed(run_1555, run_1550)  # the counterfactual
    s = _sensor()
    seen = []
    for path in RUNS:
        a = _apply(s, path.stem[13:25])
        seen.append((a["run_datetime"][11:16], a))
    assert not any(a["readiness_signal"] for _, a in seen)
    suppressed = [r for r, a in seen if a["readiness_signal_suppressed"]]
    assert suppressed[0] == "15:50" and suppressed[-1] == "16:30"
    assert seen[0][1]["readiness_signal_suppressed"] is False


def test_one_log_line_per_run_while_the_guard_holds(caplog):
    """Rule 5: one WARNING per run while the flag holds, naming the
    constraint, its RHS and violation."""
    s = _sensor()
    with caplog.at_level(logging.WARNING, logger=sp.__name__):
        for path in RUNS:
            _apply(s, path.stem[13:25])
    lines = [r.getMessage() for r in caplog.records if "#1778" in r.getMessage()]
    assert len(lines) == 9  # 15:50 ... 16:30
    assert "CA_BRIS_59E2F52D_1" in lines[0] and "1563" in lines[0]
    assert "CA_BRIS_59E2FBCC_1" in lines[-1]


def test_sensor_reads_dispatch_from_the_regional_spot_sensor():
    """Rule (b) end to end: the sensor reads the regional current price #452
    already uses, places it in the interval it was written in, and flags."""
    s = _sensor("sensor.aemo_nem_qld1_current_5min_period_price")
    run, _published = _run("202610151615")
    s.apply("PUBLIC_P5MIN_202610151615_20261015161040.zip", _no_constraints(run))
    assert not s.extra_state_attributes["false_spike_guard"]["active"]
    spot = SimpleNamespace(
        state="0.10255",
        attributes={"unit_of_measurement": "$/kWh"},
        last_updated=datetime(2026, 10, 15, 6, 10, 41, tzinfo=UTC),
    )
    hass = MagicMock()
    hass.states.get = MagicMock(
        side_effect=lambda eid: spot if eid.startswith("sensor.aemo") else None
    )
    s.hass = hass
    s.async_write_ha_state = MagicMock()
    s.check_dispatch(datetime(2026, 10, 15, 6, 11, 0, tzinfo=UTC))
    guard = s.extra_state_attributes["false_spike_guard"]
    assert guard["active"] and guard["reason"] == "dispatch_disagrees"
    assert s.extra_state_attributes["forecast_at_dispatch"][-1]["dispatch"] == 0.10255
    s.async_write_ha_state.assert_called_once()
