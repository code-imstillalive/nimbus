"""nimbus #1778 (Mark Purcell): distrust a P5MIN spike that an infeasible run
produced, or that dispatch contradicted.

Fixtures are real NEMWEB files from 15 Oct 2026 (public AEMO market data),
`tests/fixtures/p5min/2026-10-15/`: P5MIN runs 15:45-16:50 and DISPATCHIS
15:45-16:50, cut to QLD1's prices, CASESOLUTION's violation totals, the
violated CONSTRAINTSOLUTION rows (plus three non-violated ones per run, so a
parser that forgot the filter is caught) and dispatch's CA_BRIS rows.

Every test names the rule it pins.
"""

from __future__ import annotations

import csv
import io
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import solver_writer
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import number as number_mod
from custom_components.nimbus_load import p5min, p5min_guard
from custom_components.nimbus_load import sensor_p5min as sp
from custom_components.nimbus_load.solver_inputs import prices

NEM = p5min.NEM_TIME
DIR = Path(__file__).resolve().parent / "fixtures" / "p5min" / "2026-10-15"
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


def test_rule_a_flags_the_1550_run_where_the_spike_first_appeared():
    """Rule (a): 15:45 is clean; 15:50 (published 15:46:27) is the first
    spike, with CA_BRIS_59E2F52D_1 violated. Flagged in that run, before any
    dispatch interval could contradict it."""
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151545"))
    assert not g.active
    g.on_run(*_run("202610151550"))
    assert g.active and g.reason == p5min_guard.REASON_INFEASIBLE
    assert g.since == _t("15:46:27")
    assert g.detail["constraint_id"] == "CA_BRIS_59E2F52D_1"
    assert g.detail["forecast_peak"] == 23.2
    assert g.detail["rhs"] > 0 and g.detail["violation"] > 0


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
    setting is higher; unset or non-positive is the default."""
    assert p5min_guard.threshold_kwh(5000.0) == 5.0
    assert p5min_guard.threshold_kwh(22000.0) == 20.88
    assert p5min_guard.threshold_kwh(None) == 5.0
    assert p5min_guard.threshold_kwh(0.0) == 5.0


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


def test_rule_b_thresholds():
    """Rule (b): below $1,000/MWh AND below 30% of a spike forecast."""
    assert p5min_guard.dispatch_disagrees(23.2, 0.10255, THR)
    assert not p5min_guard.dispatch_disagrees(23.2, 1.0, THR)  # not < $1,000
    assert not p5min_guard.dispatch_disagrees(3.0, 0.1, THR)  # not a spike
    assert p5min_guard.dispatch_disagrees(5.0, 0.9, THR)
    assert not p5min_guard.dispatch_disagrees(5.0, 0.9, 6.0)
    assert p5min_guard.dispatch_disagrees(2.9, 0.86, 2.0)  # 0.86 < 0.87
    assert not p5min_guard.dispatch_disagrees(2.9, 0.88, 2.0)  # not < 30%


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
    """Release rule: 60 minutes after it was raised. A still-infeasible run
    does not restart it; a clean run clears the latch."""
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
    and dispatch prints the cap. Neither rule fires, and an Amber forecast
    passes through untouched. Synthetic (the 16:15 run with its constraint
    rows removed, and a $23,200 dispatch price): no fixture holds a
    dispatch-confirmed spike."""
    run, published = _run("202610151615")
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_no_constraints(run), published)
    g.on_dispatch(_t("16:15:00"), 23.2, _t("16:10:05"))
    assert not g.active and g.latched
    grid, state = _amber_express([26.31] * 6)
    with patch.object(solver_writer, "ha_get", _ha({**state, P5: _flag(g)})):
        values, _ = solver_writer.resample_generic_price_forecast_with_coverage(
            AMBER, grid, 5000.0
        )
    assert values == [26.31] * 6


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


# ---- the solver side ---------------------------------------------------------

AMBER = "sensor.amber_express_home_general_price"
CORE = "sensor.home_general_forecast"
LV = "sensor.localvolts_v2_buy_forecast"
P5 = p5min_guard.P5MIN_ENTITY_ID


def _grid(start: datetime, n: int, step: int = 5) -> list[datetime]:
    return [start + timedelta(minutes=step * i) for i in range(n)]


def _amber_express(values, start=None, live="0.42"):
    start = start or _t("16:10:00")
    grid = _grid(start, len(values))
    forecast = [{"time": t.isoformat(), "value": v} for t, v in zip(grid, values)]
    detailed = [
        {
            "start_time": (t + timedelta(seconds=1)).isoformat(),
            "end_time": (t + timedelta(minutes=5)).isoformat(),
            "duration": 5,
        }
        for t in grid
    ]
    attrs = {
        "forecast": forecast,
        "detailedForecast": detailed,
        "unit_of_measurement": "$/kWh",
    }
    return grid, {AMBER: {"state": live, "attributes": attrs}}


def _flag(g: p5min_guard.FalseSpikeGuard) -> dict:
    return {"state": "23.2", "attributes": {p5min_guard.ATTRIBUTE: g.attribute()}}


def _active_flag() -> dict:
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(*_run("202610151550"))
    return _flag(g)


def _ha(states: dict):
    read = MagicMock(side_effect=lambda eid: states[eid])
    return read


def test_amber_express_spikes_are_replaced_with_the_live_price():
    """Act rule: while the flag holds, future spike intervals take the live
    price. The flag was raised at 15:46:27, so it holds at 16:10."""
    grid, state = _amber_express([26.31, 26.31, 26.31, 0.30, 26.31, 26.31])
    with patch.object(solver_writer, "ha_get", _ha({**state, P5: _active_flag()})):
        values, real = solver_writer.resample_generic_price_forecast_with_coverage(
            AMBER, grid, 5000.0
        )
    assert values == [26.31, 0.42, 0.42, 0.30, 0.42, 0.42]
    assert real[:5] == [True] * 5


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


def test_fallback_when_the_live_price_is_a_spike():
    """Act rule: a spiking live price is not used; the current interval's
    calm price is, else the series' median calm price, else nothing."""
    grid = _grid(_t("16:10:00"), 4)
    out, _, fb = p5min_guard.replace_spikes([0.2, 26.0, 0.4, 26.0], grid, THR, 23.2)
    assert out == [0.2, 0.2, 0.4, 0.2] and fb == 0.2
    out, _, fb = p5min_guard.replace_spikes([26.0, 26.0, 0.4, 26.0], grid, THR, None)
    assert out == [26.0, 0.4, 0.4, 0.4] and fb == 0.4
    out, n, fb = p5min_guard.replace_spikes([26.0] * 4, grid, THR, 23.2)
    assert out == [26.0] * 4 and n == 0 and fb is None


def test_amber_electric_core_forecasts_are_guarded():
    """Act rule: HA core Amber's `forecasts` rows are a guarded source."""
    grid = _grid(_t("16:10:00"), 3)
    rows = [
        {
            "start_time": (t + timedelta(seconds=1)).isoformat(),
            "end_time": (t + timedelta(minutes=5)).isoformat(),
            "duration": 5,
            "per_kwh": 26.31,
        }
        for t in grid
    ]
    states = {
        CORE: {
            "state": "0.40",
            "attributes": {"forecasts": rows, "unit_of_measurement": "$/kWh"},
        },
        P5: _active_flag(),
    }
    with patch.object(solver_writer, "ha_get", _ha(states)):
        values, _ = solver_writer.resample_generic_price_forecast_with_coverage(
            CORE, grid, 5000.0
        )
    assert values == [26.31, 0.40, 0.40]


def test_localvolts_prices_are_untouched():
    """Act rule: LocalVolts is not a guarded source -- its forecast is never
    rewritten and the flag is never even read for it."""
    grid = _grid(_t("16:10:00"), 3)
    forecast = [{"time": t.isoformat(), "value": 26.31} for t in grid]
    states = {LV: {"state": "0.42", "attributes": {"forecast": forecast}}}
    read = _ha({**states, P5: _active_flag()})
    with patch.object(solver_writer, "ha_get", read):
        values, _ = solver_writer.resample_generic_price_forecast_with_coverage(
            LV, grid, 5000.0
        )
    assert values == [26.31] * 3
    assert [c.args[0] for c in read.call_args_list] == [LV]
    assert p5min_guard.source_kind(LV, states[LV]["attributes"]) is None


def test_no_flag_or_an_expired_flag_changes_nothing():
    """Act rule: the solver checks `expires_at` itself, and without a
    threshold (any other caller) the reader is unchanged."""
    grid, state = _amber_express([26.31] * 3, start=_t("16:50:00"))
    with patch.object(solver_writer, "ha_get", _ha({**state, P5: _active_flag()})):
        # raised 15:46:27, expires 16:46:27
        values, _ = solver_writer.resample_generic_price_forecast_with_coverage(
            AMBER, grid, 5000.0
        )
    assert values == [26.31] * 3
    grid, state = _amber_express([26.31] * 3)
    read = _ha({**state, P5: _active_flag()})
    with patch.object(solver_writer, "ha_get", read):
        values, _ = solver_writer.resample_generic_price_forecast_with_coverage(
            AMBER, grid
        )
    assert values == [26.31] * 3
    assert [c.args[0] for c in read.call_args_list] == [AMBER]
    with patch.object(solver_writer, "ha_get", _ha(state)):  # no P5MIN sensor
        values, _ = solver_writer.resample_generic_price_forecast_with_coverage(
            AMBER, grid, 5000.0
        )
    assert values == [26.31] * 3


def test_the_solver_reads_the_dashboard_threshold():
    """The setting reaches the solve: the generic price branch passes
    `solver_p5min_false_spike_threshold_mwh` to the reader."""
    grid = _grid(datetime(2026, 10, 15, 6, 0, tzinfo=UTC), 3)
    reader = MagicMock(return_value=([0.1] * 3, [True] * 3))
    with patch.object(
        prices._solver_writer(), "resample_generic_price_forecast_with_coverage", reader
    ):
        prices.build_price_arrays(
            {
                "solver_import_price_sensor": "sensor.i",
                "solver_export_price_sensor": "sensor.e",
                "solver_p5min_false_spike_threshold_mwh": 7000.0,
            },
            grid,
            3,
            grid[0],
            False,
            None,
        )
    assert [c.args[2] for c in reader.call_args_list] == [7000.0, 7000.0]


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
