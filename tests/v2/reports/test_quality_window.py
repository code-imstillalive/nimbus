"""Scoring one window from recorder history (#1837).

`_compute_report_for_window()` reads the day's solar, load, battery, SoC
and prices from history, scores it with `compute_quality_report()`, and
reports what the score can be trusted for: achieved energy per battery,
whether the reconstructed SoC agrees with the real sensor, the P2P
settlement for a whole calendar day. Synthetic histories only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
import solver_shared as shared
import solver_writer as sw
from solver import elements
from solver_inputs import battery_participants as participants

TZ = sw.LOCAL_TZ
DAY = datetime(2026, 8, 24, 0, 0, tzinfo=TZ)
END = DAY + timedelta(days=1)


def flat(value, step=15):
    def rows(start, end):
        out, t = [], start
        while t < end:
            out.append((t, value(t) if callable(value) else value))
            t += timedelta(minutes=step)
        return out

    return rows


def prices(cheap=0.05, expensive=0.35, hour=17):
    return flat(lambda t: expensive if t.hour >= hour else cheap)


def cfg(**kw):
    return {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": "sensor.combined_soc",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
        **kw,
    }


@pytest.fixture
def history(monkeypatch):
    """Solar 0, load 2 kW, the battery idle, SoC 50 %, cheap overnight and
    dear from 17:00: real arbitrage for the oracle, none for an idle day."""
    series = {
        "sensor.real_solar": flat(0.0),
        "sensor.real_load": flat(2.0),
        "sensor.real_battery": flat(0.0),
        "sensor.combined_soc": flat(50.0),
        "sensor.import_price": prices(),
        "sensor.export_price": prices(0.02, 0.10),
    }
    calls = []

    def fetch(eid, start, end):
        calls.append(eid)
        rows = series.get(eid)
        return rows(start, end) if rows else []

    monkeypatch.setattr(sw, "fetch_entity_history_range", fetch)

    def offline(*_a, **_k):
        raise urllib.error.URLError("no Home Assistant in this test")

    # Every other read is one Home Assistant does not answer: refused at
    # once, not after a connect timeout per call.
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    series["_calls"] = calls
    return series


def window(c=None, start=DAY, end=END, partial=True):
    return sw._compute_report_for_window(c or cfg(), start, end, allow_partial=partial)


# --- the window's shape -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"), [(DAY, DAY), (END, DAY)], ids=["empty", "reversed"]
)
def test_a_window_with_no_length_is_not_scored(history, start, end):
    assert window(start=start, end=end) is None


def test_without_a_solar_sensor_nothing_is_read_or_scored(history):
    assert window(cfg(solver_solar_power_sensor=None)) is None
    assert history["_calls"] == []


def test_a_short_window_is_scored_only_when_partial_is_allowed(history):
    six = DAY + timedelta(hours=6)
    assert window(end=six, partial=False) is None
    scored = window(end=six, partial=True)
    assert {"epr", "j_ref", "j_ach", "j_star"} <= set(scored)


@pytest.mark.parametrize(
    ("length", "periods", "hours"),
    [
        (timedelta(minutes=30), 6, 5 / 60),  # inside the 5-minute tier
        (timedelta(hours=6), 12, 0.5),
        (timedelta(days=1), 48, 0.5),
        (timedelta(days=2), 96, 0.5),
    ],
    ids=["30min", "6h", "24h", "48h"],
)
def test_the_window_is_scored_at_the_dispatch_grids_own_resolution(
    history, monkeypatch, length, periods, hours
):
    grids = []
    real = elements.PeriodGrid

    def spy(*a, **kw):
        grids.append(kw["hours"])
        return real(*a, **kw)

    monkeypatch.setattr(sw.elements, "PeriodGrid", spy)
    assert window(end=DAY + length) is not None
    (used,) = grids
    assert (len(used), float(used[0])) == (periods, pytest.approx(hours))


# --- P2P settlement is read only for one whole calendar day ------------------------------------


SETTLEMENT = {
    "state": "ok",
    "attributes": {
        "history": {
            DAY.date().isoformat(): {"export_cost": 4.20, "export_volume": 10.0}
        }
    },
}


def test_a_calendar_day_reads_its_own_p2p_settlement(history, monkeypatch):
    monkeypatch.setattr(sw, "ha_get", lambda _e: SETTLEMENT)
    scored = window(
        cfg(solver_p2p_settlement_history_sensor="sensor.p2p"), partial=False
    )
    assert (scored["real_p2p_dollars"], scored["real_p2p_volume_kwh"]) == (4.20, 10.0)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (DAY + timedelta(hours=12), DAY + timedelta(hours=36)),  # crosses midnight
        (DAY, DAY + timedelta(hours=6)),  # part of a day
    ],
    ids=["cross_midnight", "partial_day"],
)
def test_any_other_window_never_looks_the_settlement_up(
    history, monkeypatch, start, end
):
    seen = []
    monkeypatch.setattr(shared, "ha_get", lambda e: seen.append(e) or SETTLEMENT)
    scored = window(
        cfg(solver_p2p_settlement_history_sensor="sensor.p2p"), start=start, end=end
    )
    assert (scored["real_p2p_dollars"], scored["real_p2p_volume_kwh"]) == (0.0, 0.0)
    assert "sensor.p2p" not in seen


def test_yesterdays_report_is_the_same_window_scored_whole(history):
    wrapper = sw.compute_daily_quality_report(
        cfg(), datetime(2026, 8, 25, 10, 0, tzinfo=TZ)
    )
    direct = window(partial=False)
    keys = [
        "epr",
        "theoretical_maximum_yield",
        "value_captured",
        "uplift_available",
        "j_ref",
        "j_ach",
        "j_star",
        "regret_dollars",
        "tracking_fidelity",
        "tracking_cost",
        "real_p2p_dollars",
        "real_p2p_volume_kwh",
        "j_ref_hourly",
        "j_ach_hourly",
        "j_star_hourly",
        "hourly_regret",
    ]
    assert {k: wrapper[k] for k in keys} == {k: direct[k] for k in keys}


# --- achieved energy, home battery and fleet (#532, #858) ---------------------------------------


@pytest.mark.parametrize(
    ("kw", "into", "out_of"),
    [(0.0, 0.0, 0.0), (20.0, 0.0, 480.0), (-20.0, 480.0, 0.0)],
    ids=["idle", "discharge", "charge"],
)
def test_achieved_energy_is_integrated_from_the_battery_power_sensor(
    history, kw, into, out_of
):
    history["sensor.real_battery"] = flat(kw)  # discharge positive
    scored = window()
    assert scored["achieved_energy_in_kwh"] == pytest.approx(into, abs=0.1)
    assert scored["achieved_energy_out_kwh"] == pytest.approx(out_of, abs=0.1)


def _participant(name, charge_kw, discharge_kw, n):
    battery = elements.BatteryConfig(
        name=name,
        capacity_kwh=60.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=3.0,
        max_soc_kwh=60.0,
        max_charge_kw=11.0,
        max_discharge_kw=11.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.1,
    )
    return (
        battery,
        np.full(n, float(charge_kw)),
        np.full(n, float(discharge_kw)),
        30.0,
        [],
    )


@pytest.fixture
def fleet(history, monkeypatch):
    evs = []
    monkeypatch.setattr(
        participants,
        "_resolve_battery_participant_history",
        lambda *, n_periods, **_kw: [
            _participant(n, c, d, n_periods) for n, c, d in evs
        ],
    )
    history["sensor.real_battery"] = flat(20.0)
    return evs


def test_without_participants_the_fleet_is_the_home_battery(fleet):
    scored = window()
    assert scored["fleet_achieved_energy_out_kwh"] == pytest.approx(
        scored["achieved_energy_out_kwh"]
    )
    assert scored["fleet_achieved_energy_in_kwh"] == pytest.approx(
        scored["achieved_energy_in_kwh"]
    )
    (only,) = scored["achieved_energy_by_battery"].values()
    assert only["out_kwh"] == pytest.approx(scored["achieved_energy_out_kwh"])


def test_participants_count_in_the_fleet_and_by_battery_never_in_the_home_pair(fleet):
    home_only = window()
    fleet += [("ev_a", 5.0, 0.0), ("ev_b", 2.0, 0.0), ("ev_bad", 1500.0, 0.0)]
    scored = window()
    assert scored["fleet_achieved_energy_out_kwh"] == pytest.approx(480.0, abs=0.1)
    by = scored["achieved_energy_by_battery"]
    assert len(by) == 4
    assert (by["ev_a"]["in_kwh"], by["ev_b"]["in_kwh"]) == (
        pytest.approx(120.0, abs=0.1),
        pytest.approx(48.0, abs=0.1),
    )
    assert (
        by["ev_bad"]["in_kwh"] > 30000.0
    )  # a corrupt participant is named, not hidden
    assert scored["fleet_achieved_energy_in_kwh"] > 30000.0
    assert (
        scored["achieved_energy_in_kwh"] == home_only["achieved_energy_in_kwh"] == 0.0
    )
    assert scored["achieved_energy_out_kwh"] == pytest.approx(
        home_only["achieved_energy_out_kwh"]
    )


# --- whether the reconstructed SoC agrees with the real sensor (#532, #533, #538) -----------------


def _burst(history):
    """3 kW for 3 h then idle: the reconstruction settles ~18 points from a
    flat 50 % sensor without ever leaving [0, 100]."""
    history["sensor.real_battery"] = flat(
        lambda t: 3.0 if t < DAY + timedelta(hours=3) else 0.0
    )


@pytest.mark.parametrize(
    ("shape", "reliable", "reason"),
    [
        ("idle", True, None),
        ("out_of_range", False, "out_of_range"),  # 20 kW all day against 50 kWh
        ("disagreement", False, "disagreement"),
    ],
    ids=["idle", "out_of_range", "disagreement"],
)
def test_the_score_says_whether_its_soc_can_be_trusted_and_why_not(
    history, shape, reliable, reason
):
    if shape == "out_of_range":
        history["sensor.real_battery"] = flat(20.0)
    elif shape == "disagreement":
        _burst(history)
    scored = window()
    assert scored["soc_discrepancy_reliable"] is reliable
    assert scored["epr_reliable"] is reliable
    assert scored["soc_discrepancy_reason"] == reason
    if shape == "disagreement":
        assert (
            scored["soc_discrepancy_max_pct"] > 15.0
            and scored["soc_discrepancy_mean_pct"] > 8.0
        )


def test_a_household_can_accept_a_noisier_soc_sensor(history):
    _burst(history)
    scored = window(
        cfg(
            solver_soc_discrepancy_max_threshold_pct=50.0,
            solver_soc_discrepancy_mean_threshold_pct=50.0,
        )
    )
    assert scored["soc_discrepancy_reliable"] and scored["epr_reliable"]
    assert scored["soc_discrepancy_reason"] is None


# --- a SoC reading outside the envelope is clamped, never raised (#325) -------------------------


@pytest.mark.parametrize(
    ("soc", "max_pct"),
    [(3.85, 100.0), (0.0, 100.0), (104.0, 95.0)],
    ids=["below_floor", "zero", "above_ceiling"],
)
def test_a_soc_reading_outside_the_envelope_still_scores_the_whole_report(
    history, soc, max_pct
):
    healthy = window()
    history["sensor.combined_soc"] = flat(soc)
    clamped = window(cfg(solver_battery_max_soc_percent=max_pct))
    assert clamped is not None and set(clamped) == set(healthy)


@pytest.mark.parametrize(
    ("setting", "soc"),
    [
        ("solver_battery_min_soc_percent", 70.0),
        ("solver_battery_max_soc_percent", 20.0),
    ],
)
def test_the_efficiency_backtest_starts_inside_any_envelope(history, setting, soc):
    """It starts from 50 %, outside a 60 % floor or a 40 % ceiling."""
    history["sensor.combined_soc"] = flat(soc)
    value = 60.0 if setting.endswith("min_soc_percent") else 40.0
    result = sw.compute_efficiency_backtest_report(cfg(**{setting: value}), END)
    assert result is None or isinstance(result, dict)


# --- every skip says why (#313, #314) ---------------------------------------------------------


def test_every_reason_a_window_is_not_scored_is_logged_at_its_level(
    history, monkeypatch, caplog
):
    with caplog.at_level(logging.DEBUG, logger=sw._LOGGER.name):
        assert window(cfg(solver_solar_power_sensor=None)) is None
        assert window(end=DAY + timedelta(hours=6), partial=False) is None
    assert (
        "Missing sensor config" in caplog.text and "shorter than the 24" in caplog.text
    )
    caplog.clear()
    for eid in ("sensor.real_solar", "sensor.real_load", "sensor.real_battery"):
        history[eid] = lambda s, e: []
    with caplog.at_level(logging.INFO, logger=sw._LOGGER.name):
        assert window(partial=False) is None
    assert any(
        "Real history missing" in r.getMessage() and "solar=0" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


def test_an_oracle_that_cannot_solve_is_a_warning_naming_the_soc_bounds(
    history, monkeypatch, caplog
):
    def infeasible(**_kw):
        raise RuntimeError("infeasible")

    monkeypatch.setattr(sw, "compute_quality_report", infeasible)
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        assert window(partial=False) is None
    (line,) = [
        r.getMessage()
        for r in caplog.records
        if "Oracle LP infeasible" in r.getMessage()
    ]
    assert all(k in line for k in ("initial_soc=", "min_soc=", "max_soc="))


# --- a value the recorder held is not a gap (#1477) -------------------------------------------


DUSK = DAY.replace(hour=17, minute=39, second=23)


@pytest.fixture
def quiet_evening(history, monkeypatch):
    solar = history["sensor.real_solar"]
    history["sensor.real_solar"] = lambda s, e: [p for p in solar(s, e) if p[0] <= DUSK]
    sw._COVERAGE_SKIP_COUNTS.clear()
    probes = []
    answer = {"went": False}

    def probe(eid, after, end):
        probes.append((eid, after))
        return answer["went"]

    monkeypatch.setattr(
        quality_module().solver_shared, "fetch_entity_went_unavailable", probe
    )
    return probes, answer


def quality_module():
    import sys

    return sys.modules[sw._compute_report_for_window.__module__]


def test_a_sensor_that_merely_held_its_value_counts_to_the_end_of_the_day(
    quiet_evening,
):
    probes, _ = quiet_evening
    assert window(partial=False) is not None
    ((eid, after),) = probes
    assert eid == "sensor.real_solar" and after <= DUSK + timedelta(minutes=15)


@pytest.mark.parametrize("went", [True, None], ids=["went_unavailable", "unreadable"])
def test_a_real_outage_or_an_unreadable_recorder_is_still_a_gap(
    quiet_evening, caplog, went
):
    _, answer = quiet_evening
    answer["went"] = went
    with caplog.at_level(logging.INFO, logger=sw._LOGGER.name):
        assert window(partial=False) is None
    if went:
        assert "skip #1" in caplog.text


def test_full_coverage_never_pays_for_the_outage_read(history, monkeypatch):
    probes = []
    monkeypatch.setattr(
        quality_module().solver_shared,
        "fetch_entity_went_unavailable",
        lambda *a: probes.append(a) or False,
    )
    window(partial=False)
    assert probes == []


class _Response:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize(
    ("rows", "error", "expected"),
    [
        ([("unavailable", DUSK + timedelta(hours=2))], None, True),
        ([("0.0", DUSK + timedelta(hours=1))], None, False),
        ([], None, False),
        (
            [("unavailable", DUSK - timedelta(hours=1))],
            None,
            False,
        ),  # before the window
        (None, urllib.error.URLError("down"), None),  # unknown, not fine
    ],
    ids=["outage", "held", "nothing", "outage_before", "read_failed"],
)
def test_the_outage_probe_reads_only_states_after_the_last_row(
    monkeypatch, rows, error, expected
):
    payload = [[{"state": s, "last_changed": w.isoformat()} for s, w in (rows or [])]]
    monkeypatch.setattr(sw.NATIVE, "hass", None)
    monkeypatch.setattr(sw, "_load_token", lambda: "t")

    def urlopen(*_a, **_k):
        if error:
            raise error
        return _Response(payload)

    monkeypatch.setattr(shared.urllib.request, "urlopen", urlopen)
    assert shared.fetch_entity_went_unavailable("sensor.pv", DUSK, END) is expected


# --- the coverage gate names the short sensor and escalates (#984, #1054) -----------------------


C0 = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
C1 = C0 + timedelta(hours=24)


def _span(first, last, step=5):
    out, t = [], C0 + timedelta(hours=first)
    while t <= C0 + timedelta(hours=last):
        out.append((t, 1.0))
        t += timedelta(minutes=step)
    return out


def test_each_series_reports_its_own_covered_span():
    got = sw._history_coverage_by_series(
        {
            "solar": _span(0, 24),
            "battery": _span(0, 17),
            "load": _span(6, 24),
            "none": [],
            "one": [(C0 + timedelta(hours=3), 1.0)],
            "wide": [(C0 - timedelta(hours=5), 1.0), (C1 + timedelta(hours=5), 1.0)],
        },
        C0,
        C1,
    )
    hours = [got[k].hours for k in ("solar", "battery", "load", "none", "one")]
    assert hours == pytest.approx([24, 17, 18, 0, 0])
    assert (got["none"].first, got["none"].last) == (None, None)
    assert got["wide"].hours == pytest.approx(24.0)
    assert (got["wide"].first, got["wide"].last) == (C0, C1)


def test_the_gate_acts_on_the_smallest_span_and_zero_when_any_is_empty():
    spans = (_span(0, 24), _span(0, 17), _span(6, 24))
    assert sw._history_coverage_hours(spans, C0, C1) == pytest.approx(17.0)
    assert sw._history_coverage_hours((_span(0, 24), [], _span(0, 24)), C0, C1) == 0.0
    assert sw._history_coverage_hours((), C0, C1) == 0.0


@pytest.fixture
def short_battery(history):
    """16 Sep's shape: solar and load whole, the battery stopping at 17:01."""
    history["sensor.real_battery"] = lambda s, e: flat(0.0)(
        s, s + timedelta(hours=17, minutes=1)
    )
    sw._COVERAGE_SKIP_COUNTS.clear()
    yield history
    sw._COVERAGE_SKIP_COUNTS.clear()


def _skip(caplog, start=DAY):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=sw._LOGGER.name):
        assert window(start=start, end=start + timedelta(days=1), partial=False) is None
    (rec,) = [r for r in caplog.records if "Nimbus quality: skip" in r.getMessage()]
    return rec


def test_the_skip_line_names_every_sensor_shortest_first_with_its_span(
    short_battery, caplog
):
    line = _skip(caplog).getMessage()
    assert "only 17.00 h of the 24.00 h window" in line
    assert "skip #1 for 2026-08-24" in line
    assert "sensor.real_battery 17.00 h [00:00-17:00]" in line
    assert "sensor.real_solar 23.75 h" in line and "sensor.real_load 23.75 h" in line
    assert line.index("sensor.real_battery") < line.index("sensor.real_solar")


def test_an_empty_sensor_is_named_by_the_row_count_guard_before_it(
    short_battery, caplog
):
    short_battery["sensor.real_battery"] = lambda s, e: []
    with caplog.at_level(logging.INFO, logger=sw._LOGGER.name):
        assert window(partial=False) is None
    assert any(
        "Real history missing" in r.getMessage() and "battery=0 rows" in r.getMessage()
        for r in caplog.records
    )


def test_a_day_that_keeps_failing_becomes_a_warning_counted_per_day(
    short_battery, caplog
):
    after = sw._COVERAGE_SKIP_WARN_AFTER
    levels = [_skip(caplog).levelname for _ in range(after + 2)]
    assert levels[: after - 1] == ["INFO"] * (after - 1)
    assert levels[after - 1 :] == ["WARNING"] * 3
    assert _skip(caplog, start=DAY - timedelta(days=1)).levelname == "INFO"


# --- stale price-source data is kept out of the score (#1825) ---------------------------------


pf = sw.price_freshness
OCT16 = datetime(2026, 10, 16, tzinfo=TZ)
GRID = [OCT16 + i * timedelta(minutes=30) for i in range(48)]
STALL = ["11:30", "12:00", "12:30", "13:00"]


def _at(h, m, s=0):
    return OCT16.replace(hour=h, minute=m, second=s)


def _sixteen_october():
    """(recorded at, stamp): fresh every 5 min, the 10:01 stamp held from
    10:35 to 13:01, then an evening the recorder keeps no rows for."""
    rows, t = [], _at(0, 0, 30)
    while t < _at(10, 35):
        rows.append((t, t - timedelta(seconds=25)))
        t += timedelta(minutes=5)
    t = _at(10, 35, 52)
    while t < _at(13, 1):
        rows.append((t, _at(10, 1, 48)))
        t += timedelta(minutes=5)
    t = _at(13, 1, 33)
    while t < _at(17, 0):
        rows.append((t, t - timedelta(seconds=40)))
        t += timedelta(minutes=5)
    return [*rows, (_at(17, 0, 12), _at(16, 33, 19)), (_at(20, 15, 18), _at(20, 2, 57))]


def _labels(mask):
    return [f"{GRID[i]:%H:%M}" for i, flag in enumerate(mask) if flag]


def test_only_the_stall_is_stale_never_a_held_evening_and_the_oldest_stamp_decides():
    assert _labels(pf.stale_period_mask([_sixteen_october()], GRID)) == STALL
    fresh = [(gt, gt - timedelta(minutes=1)) for gt in GRID]
    assert _labels(pf.stale_period_mask([fresh, _sixteen_october()], GRID)) == STALL
    assert pf.stale_period_mask([], GRID) == [False] * 48
    assert pf.stale_period_mask([[]], GRID) == [False] * 48


def test_the_threshold_is_the_solvers_own_and_windows_are_local():
    t = _at(12, 0)
    assert pf.stale_period_mask([[(t, t - pf.STALE_AFTER)]], [t]) == [False]
    over = t - pf.STALE_AFTER - timedelta(seconds=1)
    assert pf.stale_period_mask([[(t, over)]], [t]) == [True]
    half = timedelta(minutes=30)
    mask = pf.stale_period_mask([_sixteen_october()], GRID)
    assert pf.stale_windows(GRID, mask, half, sw._local) == ["11:30-13:30"]
    last = [False] * 47 + [True]
    assert pf.stale_windows(GRID, last, half, sw._local) == ["23:30-00:00"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-16T00:01:48Z", datetime.fromisoformat("2026-10-16T00:01:48+00:00")),
        ("2026-10-16T00:01:48", None),  # naive
        (12.5, None),
    ],
)
def test_stamps_parse_as_the_recorder_stores_them(value, expected):
    assert pf.parse_stamp(value) == expected


LV_SIBLINGS = [
    "sensor.localvolts_v2_current_buy_rate",
    "sensor.localvolts_v2_current_sell_rate",
]
LV_CFG = {
    "solver_import_price_sensor": "sensor.localvolts_v2_buy_flex_up",
    "solver_export_price_sensor": "sensor.localvolts_v2_sell_flex_up",
    "solver_regional_spot_current_price_sensor": "sensor.aemo_qld1_price",
    "known_sources": {
        "localvolts_v2": {
            "entities": [
                "sensor.localvolts_v2_buy_flex_up",
                "sensor.localvolts_v2_sell_flex_up",
                *LV_SIBLINGS,
            ],
            "stamp_siblings": LV_SIBLINGS,
        }
    },
}
SIB = ("sensor.localvolts_v2_current_buy_rate", "lastUpdate")


def _reprice(stamps, aemo, *, boom=False):
    calls = []

    def attribute_history(eid, attribute, start, end, convert=float):
        if boom:
            raise RuntimeError("recorder gone")
        calls.append(eid)
        return stamps.get((eid, attribute), [])

    fake = SimpleNamespace(
        price_freshness=pf,
        fetch_entity_attribute_history_range=attribute_history,
        fetch_entity_history_range=lambda *_a, **_k: aemo,
        resample_history_nearest=sw.resample_history_nearest,
        compute_5min_offset=lambda hist, **_k: dict.fromkeys(range(288), 0.04),
        fetch_price_history=lambda *_a, **_k: [(OCT16, 0.1)],
        import_fee_rate=lambda _c, _h: 0.10,
        _local=sw._local,
    )
    imp, exp = np.full(48, 0.30), np.full(48, 0.20)
    imp[23:27] = [0.36, 1.12, 1.12, 10.5]  # the withdrawn spike
    result = quality_module()._reprice_stale_price_periods(
        fake, LV_CFG, GRID, 0.5, OCT16, OCT16 + timedelta(days=1), imp, exp
    )
    return result, imp, exp, calls


def test_the_stale_spike_is_repriced_from_aemo_and_nothing_else_moves():
    result, imp, exp, calls = _reprice({SIB: _sixteen_october()}, [(OCT16, 0.05)])
    stale = [23, 24, 25, 26]
    np.testing.assert_allclose(imp[stale], 0.05 + 0.04 + 0.10)
    np.testing.assert_allclose(exp[stale], 0.05 + 0.04)
    np.testing.assert_allclose(np.delete(imp, stale), 0.30)
    np.testing.assert_allclose(np.delete(exp, stale), 0.20)
    assert result["price_stale_periods"] == 4
    assert result["price_stale_minutes"] == 120
    assert result["price_stale_windows"] == ["11:30-13:30"]
    assert result["price_stale_repriced"] and not result["price_stale_unusable"]
    assert set(LV_SIBLINGS) <= set(calls)


def test_a_day_that_cannot_be_repriced_or_is_mostly_stale_is_not_a_measurement():
    result, imp, _exp, _calls = _reprice({SIB: _sixteen_october()}, [])
    assert result["price_stale_unusable"] and not result["price_stale_repriced"]
    assert imp[26] == 10.5
    start = _at(6, 0)
    frozen = [
        (start + timedelta(minutes=5 * k), start - timedelta(hours=2))
        for k in range(84)
    ]
    result, *_ = _reprice({SIB: frozen}, [(OCT16, 0.05)])
    assert result["price_stale_periods"] > 12
    assert result["price_stale_repriced"] and result["price_stale_unusable"]


def test_a_fresh_day_or_a_failing_check_changes_nothing(caplog):
    fresh = [(gt, gt - timedelta(minutes=2)) for gt in GRID]
    result, imp, *_ = _reprice({SIB: fresh}, [(OCT16, 0.05)])
    assert result["price_stale_periods"] == 0 and imp[26] == 10.5
    with caplog.at_level(logging.WARNING):
        result, imp, *_ = _reprice({}, [], boom=True)
    assert result["price_stale_periods"] == 0 and imp[26] == 10.5
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_an_unusable_price_day_is_not_reliable_and_names_the_price_source_first():
    assert not sw._epr_reliability(True, True, None, None, True)
    assert sw._epr_reliability(True, True, None, None, False)
    assert sw._epr_reliability(True, True)  # older callers
    entry = {
        "epr_reliable": False,
        "regret_reliable": False,
        "price_stale_unusable": True,
    }
    assert sw._epr_reliability_code(entry) == sw._RELIABILITY_PRICE


# --- the oracle keeps a fixed P2P export block (2026-09-01, #1001) ------------------------------


def _bell(t):
    hour = t.hour + t.minute / 60.0
    return 30.0 * max(0.0, 1 - ((hour - 12) / 6) ** 2) if 6 <= hour <= 18 else 0.0


@pytest.fixture
def p2p_day(history):
    history["sensor.real_solar"] = flat(_bell)
    history["sensor.real_load"] = flat(1.0)
    history["sensor.import_price"] = flat(lambda t: 0.55 if 17 <= t.hour < 20 else 0.15)
    history["sensor.export_price"] = flat(lambda t: 0.11 if 17 <= t.hour < 20 else 0.05)
    return cfg(
        solver_battery_capacity_kwh=122.2,
        solver_max_charge_kw=40.0,
        solver_max_discharge_kw=40.0,
        solver_salvage_value=0.0,
        solver_grid_max_import_kw=42.0,
        solver_grid_max_export_kw=42.0,
    )


def test_the_oracle_never_exports_past_a_fixed_p2p_block_and_is_free_outside_it(
    p2p_day,
):
    assert window(p2p_day, partial=False) is not None  # no block: unaffected
    block = {
        **p2p_day,
        "solver_p2p_block_1_rate_kw": 11.5,
        "solver_p2p_block_1_start_hour": 17,
        "solver_p2p_block_1_end_hour": 24,
    }
    rows = window(block, partial=False)["j_star_hourly"]
    for hour in range(17, 24):
        assert (
            rows[(DAY + timedelta(hours=hour)).isoformat()]["grid_kw"] >= -11.5 - 0.05
        )
    assert rows[(DAY + timedelta(hours=3)).isoformat()]["grid_kw"] != pytest.approx(
        -11.5, abs=0.05
    )


# --- the battery sensor's sign (#299) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("reading", "flag", "charge", "discharge"),
    [
        (5.0, None, 0.0, 5.0),  # the default: positive is discharge
        (5.0, False, 0.0, 5.0),
        (5.0, True, 5.0, 0.0),  # a positive-is-charge sensor
        (-5.0, True, 0.0, 5.0),
    ],
    ids=["default", "explicit_false", "flipped", "flipped_negative"],
)
def test_the_battery_reading_is_read_with_the_configured_sign(
    history, monkeypatch, reading, flag, charge, discharge
):
    history["sensor.real_battery"] = flat(reading)
    history["sensor.real_load"] = flat(3.0)
    seen = {}
    real = sw.compute_quality_report

    def spy(**kw):
        seen["charge"], seen["discharge"] = (
            kw["actual_charge_kw"][0],
            kw["actual_discharge_kw"][0],
        )
        return real(**kw)

    monkeypatch.setattr(sw, "compute_quality_report", spy)
    extra = {} if flag is None else {"solver_battery_power_positive_is_charge": flag}
    assert sw.compute_daily_quality_report(
        cfg(**extra), datetime(2026, 8, 25, 10, 0, tzinfo=TZ)
    )
    assert (seen["charge"] == charge).all() and (seen["discharge"] == discharge).all()
