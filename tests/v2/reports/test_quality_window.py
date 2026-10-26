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


def test_a_window_with_no_length_is_not_scored(history, monkeypatch, caplog):
    # test_a_window_with_no_length_is_not_scored
    for start, end in [(DAY, DAY), (END, DAY)]:
        assert window(start=start, end=end) is None

    # test_the_window_is_scored_at_the_dispatch_grids_own_resolution
    with monkeypatch.context() as mp:
        for length, periods, hours in [
            (timedelta(minutes=30), 6, 5 / 60),  # inside the 5-minute tier
            (timedelta(hours=6), 12, 0.5),
            (timedelta(days=1), 48, 0.5),
            (timedelta(days=2), 96, 0.5),
        ]:
            with mp.context() as mp:
                grids = []
                real = elements.PeriodGrid

                def spy(*a, **kw):
                    grids.append(kw["hours"])  # noqa: B023
                    return real(*a, **kw)  # noqa: B023

                mp.setattr(sw.elements, "PeriodGrid", spy)
                assert window(end=DAY + length) is not None
                (used,) = grids
                assert (len(used), float(used[0])) == (periods, pytest.approx(hours))

    # test_a_soc_reading_outside_the_envelope_still_scores_the_whole_report
    for soc, max_pct in [(3.85, 100.0), (0.0, 100.0), (104.0, 95.0)]:
        healthy = window()
        history["sensor.combined_soc"] = flat(soc)
        clamped = window(cfg(solver_battery_max_soc_percent=max_pct))
        assert clamped is not None and set(clamped) == set(healthy)

    # test_the_efficiency_backtest_starts_inside_any_envelope: It starts from 50 %, outside a 60 % floor or a 40 % ceiling.
    for setting, soc in [
        ("solver_battery_min_soc_percent", 70.0),
        ("solver_battery_max_soc_percent", 20.0),
    ]:
        history["sensor.combined_soc"] = flat(soc)
        value = 60.0 if setting.endswith("min_soc_percent") else 40.0
        result = sw.compute_efficiency_backtest_report(cfg(**{setting: value}), END)
        assert result is None or isinstance(result, dict)

    # test_every_reason_a_window_is_not_scored_is_logged_at_its_level
    caplog.clear()
    with monkeypatch.context() as mp:
        with caplog.at_level(logging.DEBUG, logger=sw._LOGGER.name):
            assert window(cfg(solver_solar_power_sensor=None)) is None
            assert window(end=DAY + timedelta(hours=6), partial=False) is None
        assert (
            "Missing sensor config" in caplog.text
            and "shorter than the 24" in caplog.text
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


# --- P2P settlement is read only for one whole calendar day ------------------------------------


SETTLEMENT = {
    "state": "ok",
    "attributes": {
        "history": {
            DAY.date().isoformat(): {"export_cost": 4.20, "export_volume": 10.0}
        }
    },
}


# --- achieved energy, home battery and fleet (#532, #858) ---------------------------------------


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


# --- whether the reconstructed SoC agrees with the real sensor (#532, #533, #538) -----------------


def _burst(history):
    """3 kW for 3 h then idle: the reconstruction settles ~18 points from a
    flat 50 % sensor without ever leaving [0, 100]."""
    history["sensor.real_battery"] = flat(
        lambda t: 3.0 if t < DAY + timedelta(hours=3) else 0.0
    )


# --- a SoC reading outside the envelope is clamped, never raised (#325) -------------------------


# --- every skip says why (#313, #314) ---------------------------------------------------------


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


def test_the_gate_acts_on_the_smallest_span_and_zero_when_any_is_empty(
    short_battery, caplog
):
    # test_the_gate_acts_on_the_smallest_span_and_zero_when_any_is_empty
    spans = (_span(0, 24), _span(0, 17), _span(6, 24))
    assert sw._history_coverage_hours(spans, C0, C1) == pytest.approx(17.0)
    assert sw._history_coverage_hours((_span(0, 24), [], _span(0, 24)), C0, C1) == 0.0
    assert sw._history_coverage_hours((), C0, C1) == 0.0

    # test_a_day_that_keeps_failing_becomes_a_warning_counted_per_day
    caplog.clear()
    after = sw._COVERAGE_SKIP_WARN_AFTER
    levels = [_skip(caplog).levelname for _ in range(after + 2)]
    assert levels[: after - 1] == ["INFO"] * (after - 1)
    assert levels[after - 1 :] == ["WARNING"] * 3
    assert _skip(caplog, start=DAY - timedelta(days=1)).levelname == "INFO"

    # test_a_day_that_cannot_be_repriced_or_is_mostly_stale_is_not_a_measurement
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

    # test_a_fresh_day_or_a_failing_check_changes_nothing
    caplog.clear()
    fresh = [(gt, gt - timedelta(minutes=2)) for gt in GRID]
    result, imp, *_ = _reprice({SIB: fresh}, [(OCT16, 0.05)])
    assert result["price_stale_periods"] == 0 and imp[26] == 10.5
    with caplog.at_level(logging.WARNING):
        result, imp, *_ = _reprice({}, [], boom=True)
    assert result["price_stale_periods"] == 0 and imp[26] == 10.5
    assert any(r.levelno == logging.WARNING for r in caplog.records)


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


# --- each controllable load's delivery is published and moves no score (#768) ------------------


HWS = {
    "controllable_load_name": "Hot water",
    "controllable_load_kind": "deferrable",
    "controllable_load_device_entity": "water_heater.wwk302",
    "controllable_load_power_sensor": "sensor.wwk302_power",
    "deferrable_max_power_kw": 0.65,
    "deferrable_target_kwh": 4.0,
}


def _hass(*subentries):
    entry = SimpleNamespace(
        entry_id="hub", subentries={s.subentry_id: s for s in subentries}
    )
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _d: [entry]),
        states=SimpleNamespace(get=lambda _e: None),
    )


@pytest.fixture
def delivery(history, monkeypatch):
    """0.65 kW through hours 2-4 and 0 the rest of the day: 1.95 kWh."""
    history["sensor.real_battery"] = flat(1.0)
    on = (DAY + timedelta(hours=2), DAY + timedelta(hours=5))
    monkeypatch.setattr(
        sw,
        "fetch_entity_power_history_kw",
        lambda _e, s, e: flat(lambda t: 0.65 if on[0] <= t < on[1] else 0.0, step=5)(
            s, e
        ),
    )

    def run(hass):
        monkeypatch.setattr(sw.NATIVE, "hass", hass)
        return window()

    return run


def test_a_configured_load_is_published_with_what_it_really_delivered(delivery):
    hws = SimpleNamespace(
        subentry_id="s_hws", subentry_type="controllable_load", data=HWS
    )
    (entry,) = delivery(_hass(hws))["controllable_load_delivery"]
    assert (
        entry["subentry_id"],
        entry["name"],
        entry["kind"],
        entry["power_sensor"],
    ) == (
        "s_hws",
        "Hot water",
        "deferrable",
        "sensor.wwk302_power",
    )
    assert entry["scorable"] and entry["delivered_kwh"] == pytest.approx(1.95, abs=1e-4)


# --- how much of the regret is the two pricings disagreeing (#1162) ----------------------------


def test_the_pricing_share_of_the_regret(no_soc, caplog, history, monkeypatch):
    # test_the_pricing_share_of_the_regret
    for regret, delta, share in [
        (3.6485, 2.6259, 0.7197),  # most of the headline is the pricing, not dispatch
        (-0.7855, 0.0, 0.0),
        (3.6485, None, 0.0),
        (0.0, 2.6259, 0.0),  # a share of nothing is not a finding
        (1e-12, 2.6259, 0.0),
        (-2.0, 1.0, 0.5),  # magnitude: a negative regret does not flip it
        (2.0, -1.0, 0.5),
        (1.0, 5.0, 1.0),  # a share, never past the whole
        (30.0, 15.0, 0.5),
    ]:
        assert sw._regret_path_delta_share(regret, delta) == pytest.approx(
            share, abs=1e-3
        )

    # test_the_refusal_is_quiet_at_first_and_loud_when_it_persists
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=sw._LOGGER.name):
        for _ in range(sw._COVERAGE_SKIP_WARN_AFTER):
            window()
    lines = [r for r in caplog.records if "SoC sensor" in r.getMessage()]
    assert (lines[0].levelno, lines[-1].levelno) == (logging.INFO, logging.WARNING)
    assert "sensor.combined_soc" in lines[0].getMessage()
    assert DAY.date().isoformat() in lines[0].getMessage()

    # test_the_window_is_checked_against_the_households_grid_meter
    for key, metered, status in [
        ("switchboard_grid_meter_sensor", 2.0, "agrees"),  # load 2 kW from the grid
        ("switchboard_grid_meter_sensor", 9.0, "disagrees"),
        ("grid_sensor", 9.0, "disagrees"),  # the Forecaster's grid sensor, failing that
    ]:
        history["sensor.grid_meter"] = flat(metered)
        scored = window(cfg(**{key: "sensor.grid_meter"}))
        assert scored["meter_reconciliation"]["status"] == status
        if status == "disagrees":
            assert scored["epr_reliable"] is False
            assert scored["epr_reason"] == "grid_meter_disagrees"

    # test_the_window_is_priced_on_the_repriced_series_and_an_unusable_day_says_so
    with monkeypatch.context() as mp:
        from solver_reports import quality as q

        base = window()

        def reprice(_sw, _cfg, _grid, _hours, _start, _end, imp, exp):
            imp[:] = imp * 3.0  # the price the score must then be priced on
            return {
                "price_stale_periods": 48,
                "price_stale_minutes": 1440,
                "price_stale_windows": ["00:00-00:00"],
                "price_stale_repriced": False,
                "price_stale_unusable": True,
            }

        mp.setattr(q, "_reprice_stale_price_periods", reprice)
        scored = window()
        assert scored["j_ref"] == pytest.approx(3.0 * base["j_ref"], rel=1e-6)
        assert scored["price_stale_unusable"] is True
        assert (
            scored["epr_reliable"] is False
            and scored["epr_reason"] == "price_source_stale"
        )


# --- a configured SoC sensor that returned nothing is no score (#1214) -------------------------


@pytest.fixture
def no_soc(history, monkeypatch):
    monkeypatch.setattr(sw, "_SOC_HISTORY_SKIP_COUNTS", {})
    history["sensor.combined_soc"] = lambda s, e: []
    return history


# --- the grid meter and stale prices reach the window's verdict (#1465, #1825) ---------------


# --- the capacity a report states (#1013, #1172) ----------------------------------------------
