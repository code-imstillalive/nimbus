"""One price forecast from several sources, each scored by its own running
error, in shadow by default and live behind a switch (#1786) (#1837).

`consolidate()` takes the median, or the weighted mean once every available
source has earned a weight; a source still being scored stays out of the
blend unless it alone forecasts a spike. Each source's error is tracked per
lead bucket with its standing offset learned as bias, not error, and
persists across a restart. Live, only future periods change and the settled
block keeps its settled price; in shadow nothing the plan schedules on moves.
The AEMO-based source can be priced by contract (`retail_from_rrp`).
Synthetic values only.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import solver_shared
import solver_writer as sw
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths
from solver_inputs import loss_factors, prices
from solver_inputs import price_consolidation as pc

NOW = datetime(2026, 10, 16, 2, 0, 30, tzinfo=UTC)


# --- consolidate() ------------------------------------------------------------------------------


def test_the_spread_is_the_raw_range_and_no_source_is_none(monkeypatch):
    # test_the_spread_is_the_raw_range_and_no_source_is_none
    # test_the_spread_is_the_raw_range_and_no_source_is_none
    _v, spread, _m = pc.consolidate(
        {"a": [1.0, 1.0], "b": [3.0, 2.0], "c": [10.0, None]}
    )
    assert spread == [9.0, 1.0]
    assert pc.consolidate({"a": [None, 2.0], "b": [None]})[1] == [None, 0.0]
    assert pc.consolidate({"a": [None]})[2] == "none"

    # test_an_unscored_source_still_takes_a_spike_it_alone_forecasts
    values, _s, method = pc.consolidate(
        {"a": [0.30, 0.30], "b": [0.25, 5.00]}, {"a": 1.0}, spike_threshold=0.85
    )
    assert values == [0.30, 5.00] and method == "mixed"

    # test_the_aemo_markup_series_is_wholesale_plus_the_learned_markup
    pts = [(NOW + timedelta(minutes=30), 0.10), (NOW + timedelta(minutes=60), 0.20)]
    grid = [NOW, NOW + timedelta(minutes=30), NOW + timedelta(minutes=90)]
    assert pc.aemo_markup_series(grid, pts, {1: 0.05}, lambda t: 1) == [
        None,
        pytest.approx(0.15),
        pytest.approx(0.25),
    ]
    assert pc.aemo_markup_series(grid, [], {}, lambda t: 0) == [None] * 3

    # test_the_blend_corrects_a_premium_but_shows_and_spikes_on_the_sources_own_price
    tr = pc.SourceErrorTracker()
    _train(tr, {"primary": 0.14, "amber": 0.16}, 0.14, 200)
    now = datetime(2026, 10, 17, 3, 0, 30, tzinfo=UTC)
    grid = [now + timedelta(minutes=2)]
    values, summary, _ = pc.consolidate_side(
        tr, {"primary": [0.15], "amber": [0.17]}, now, grid, [1 / 12], 0, None
    )
    assert values[0] == pytest.approx(0.15, abs=1e-4)
    assert summary["spread_now"] == pytest.approx(0.02)
    assert summary["next"]["sources"]["amber"] == [0.17]
    spiked, _, _ = pc.consolidate_side(
        tr, {"primary": [0.30], "amber": [1.01]}, now, grid, [1 / 12], 0, None, 1.0
    )
    assert spiked[0] == pytest.approx(1.01)

    # test_the_blend_is_scored_as_it_enters_the_plan_and_never_weighs_itself
    # #1786: the switch to `live` should rest on the blend's own measured
    # error. It is scored raw (no offset removed), and earns no weight.
    tr = pc.SourceErrorTracker()
    _train(tr, {"primary": 0.14, "amber": 0.16}, 0.14, 200)
    values, _, _ = pc.consolidate_side(
        tr, {"primary": [0.15], "amber": [0.17]}, now, grid, [1 / 12], 0, None
    )
    tr.settle(pc.interval_key(now + timedelta(minutes=5)), 0.17)
    assert tr.mae[pc.BLEND_SOURCE]["0-5 min"] == pytest.approx(0.17 - values[0])
    assert tr.samples[pc.BLEND_SOURCE]["0-5 min"] == 1
    assert pc.BLEND_SOURCE in tr.summary()["mae_by_source_and_lead"]
    assert set(tr.summary()["weights"]["0-5 min"]) == {"primary", "amber"}
    # 29 Oct 2026: a source removed from the settings has no share.
    _, shown, _ = pc.consolidate_side(
        tr, {"primary": [0.15]}, now, grid, [1 / 12], 0, None
    )
    assert set(shown["weights"]["0-5 min"]) == {"primary"}
    tr.samples[pc.BLEND_SOURCE]["0-5 min"] = 500
    assert set(tr.weights("0-5 min")) == {"primary", "amber"}

    # test_a_source_whose_sensor_changed_starts_fresh
    # 29 Oct 2026, the reference household: price source 3 moved from one
    # tariff's sensor to another and kept the old one's share and offset. A
    # slot is forgotten when its sensor changes; a slot not yet recorded
    # (older state) is only recorded; an unchanged slot keeps its score.
    tr = pc.SourceErrorTracker()
    _train(tr, {"primary": 0.14, "secondary_3": 0.18}, 0.14, 60)
    assert tr.follow_sources({"primary": "s.buy", "secondary_3": "s.tou"}) == []
    assert tr.follow_sources({"primary": "s.buy", "secondary_3": "s.tou"}) == []
    tr.record("secondary_3", now, now + timedelta(minutes=10), 0.2)
    assert tr.follow_sources({"primary": "s.buy", "secondary_3": "s.two_way"}) == [
        "secondary_3"
    ]
    assert "secondary_3" not in tr.mae and "secondary_3" not in tr.bias
    assert all("secondary_3" not in v for v in tr.pending.values())
    assert tr.samples["primary"]["0-5 min"] == 60
    assert pc.SourceErrorTracker.from_dict(tr.to_dict()).sources == {
        "primary": "s.buy",
        "secondary_3": "s.two_way",
    }
    # A state saved before the slots were checked (0.94.497 and older)
    # scores the extra sources again, once; the provider keeps its score.
    _train(tr, {"secondary_3": 0.18}, 0.14, 5)
    old = tr.to_dict()
    del old["sources_checked"]
    upgraded = pc.SourceErrorTracker.from_dict(old)
    assert "secondary_3" not in upgraded.mae and "secondary_3" not in upgraded.bias
    assert upgraded.samples["primary"]["0-5 min"] == 60
    again = pc.SourceErrorTracker.from_dict(tr.to_dict())
    assert again.samples["secondary_3"]["0-5 min"] == 5

    # test_the_tracker_round_trips_and_a_damaged_or_older_state_loads
    tr = _tracker_with(60, 70)
    _train(tr, {"amber": 0.16}, 0.14, 3)
    tr.last_interval, tr.last_settled = 123, 0.31
    assert pc.SourceErrorTracker.from_dict(tr.to_dict()).to_dict() == tr.to_dict()
    old = tr.to_dict()
    del old["bias"]
    assert pc.SourceErrorTracker.from_dict(old).bias == {}
    assert pc.SourceErrorTracker.from_dict({"mae": "junk"}).to_dict() == (
        pc.SourceErrorTracker().to_dict()
    )
    assert pc.SourceErrorTracker.from_dict(None).mae == {}

    # test_live_a_period_with_nothing_to_consolidate_keeps_its_own_price
    _, raw, _, lab_i, _ = _apply(
        True,
        n_settled=1,
        masks=[True, False, False, False],
        imp=[0.30, 0.77, 0.77, 0.77],
        labels=["primary", "fallback", "fallback", "fallback"],
    )
    assert raw == [0.30, 0.77, 0.77, 0.77]
    assert lab_i == ["primary", "fallback", "fallback", "fallback"]

    # test_a_failure_returns_none_and_touches_nothing
    with monkeypatch.context() as mp:

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        mp.setattr(pc, "consolidate", boom)
        result, raw, *_ = _apply(True, aemo=[0.9] * 5)
        assert result is None and raw == [0.30] * 5

    # test_the_trackers_persist_across_a_restart
    grid = [NOW + timedelta(minutes=5 * (i + 1)) for i in range(3)]
    p = SimpleNamespace(
        import_real_mask=[True] * 3,
        export_real_mask=[True] * 3,
        aemo_import_series=None,
        aemo_export_series=None,
    )
    with isolated_state_paths(sw):
        for now in (
            NOW,
            grid[0] + timedelta(seconds=10),
            grid[1] + timedelta(seconds=10),
        ):
            sw.apply_price_consolidation(
                {}, now, grid, [5 / 60] * 3, 0, p, [0.30] * 3, [0.05] * 3,
                0.31, 0.05, [0.30] * 3, [0.05] * 3, ["primary"] * 3, ["primary"] * 3,
            )  # fmt: skip
        sw._PRICE_CONSOLIDATION_CACHE = None  # a restart
        trackers = sw._load_price_trackers()
    assert trackers["import"].samples["primary"]["0-5 min"] == 1
    assert trackers["import"].mae["primary"]["0-5 min"] == pytest.approx(0.01)


# --- each source's running error ----------------------------------------------------------------


def _train(tr, source_values, settled, n):
    t = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    for k in range(n):
        ti = t + timedelta(minutes=5 * k)
        for name, v in source_values.items():
            tr.record(name, ti - timedelta(minutes=2), ti, v)
        tr.settle(pc.interval_key(ti), settled)


def _tracker_with(a, b):
    tr = pc.SourceErrorTracker()
    tr.mae = {"a": {"0-5 min": 0.01}, "b": {"0-5 min": 0.03}}
    tr.samples = {"a": {"0-5 min": a}, "b": {"0-5 min": b}}
    return tr


# --- apply_price_consolidation() ----------------------------------------------------------------


def _apply(live, aemo=None, n_settled=2, masks=None, imp=None, labels=None):
    grid = [NOW + timedelta(minutes=i) for i in range(n_settled)] + [
        NOW + timedelta(minutes=5 * (i + 1)) for i in range(3)
    ]
    n = len(grid)
    imp = imp or [0.30] * n
    p = SimpleNamespace(
        import_real_mask=masks or [True] * n,
        export_real_mask=[True] * n,
        aemo_import_series=aemo,
        aemo_export_series=None,
    )
    raw, exp = list(imp), [0.05] * n
    lab_i, lab_e = list(labels or ["primary"] * n), ["primary"] * n
    with isolated_state_paths(sw):
        result = sw.apply_price_consolidation(
            {"solver_price_consolidation_live": live},
            NOW,
            grid,
            [1 / 60] * n_settled + [5 / 60] * 3,
            n_settled,
            p,
            list(imp),
            [0.05] * n,
            0.30,
            0.05,
            raw,
            exp,
            lab_i,
            lab_e,
        )
    return result, raw, exp, lab_i, lab_e


# --- the real main() ----------------------------------------------------------------------------

SECONDARY = "sensor.fake_import_price_2"
SCHEDULING = (
    "import_price",
    "export_price",
    "import_price_raw",
    "export_price_raw",
    "import_price_source",
    "export_price_source",
    "battery_kw",
    "grid_import_kw",
    "grid_export_kw",
)


def _main(cfg_extra, disabled=False, monkeypatch=None):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    attrs.update({"solver_import_price_sensor_2": SECONDARY, **cfg_extra})
    base = guardrail._make_ha_get()
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    secondary = {
        "state": "0.5",
        "attributes": {
            "forecast": [
                {"time": (start + timedelta(minutes=30 * i)).isoformat(), "value": 0.50}
                for i in range(200)
            ]
        },
    }

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        return secondary if entity_id == SECONDARY else base(entity_id)

    posted = {}
    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setattr(sw, "ha_get", ha_get)
        mp.setattr(solver_shared, "ha_get", ha_get)
        mp.setattr(sw, "ha_post_state", lambda e, s, a=None: posted.__setitem__(e, a))
        if disabled:
            mp.setattr(sw, "apply_price_consolidation", lambda *a, **k: None)
        stack.enter_context(isolated_state_paths(sw))
        sw.main()
    return posted["sensor.nimbus_solver_battery_forecast"]


def _scheduling(attrs):
    return [{k: row.get(k) for k in SCHEDULING} for row in attrs["forecast"]]


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_in_shadow_schedules_exactly_as_without_consolidation(freezer):
    shadow, without = _main({}), _main({}, disabled=True)
    assert _scheduling(shadow) == _scheduling(without)
    assert shadow["total_cost"] == without["total_cost"]
    assert shadow["price_consolidation"]["import"]["sources"] == [
        "primary",
        "secondary_2",
    ]
    rows = shadow["forecast"]
    assert rows[1]["consolidated_import_price"] == pytest.approx(
        0.40
    )  # median of 0.30, 0.50
    assert rows[1]["import_price"] == pytest.approx(0.30)
    assert rows[0]["consolidated_export_price"] == pytest.approx(0.05)
    assert without["price_consolidation"] is None


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_live_moves_every_future_period_and_not_the_settled_one(freezer):
    live = _main({"solver_price_consolidation_live": True})
    rows = live["forecast"]
    assert live["price_consolidation"]["mode"] == "live"
    assert (rows[0]["import_price"], rows[0]["import_price_source"]) == (
        pytest.approx(0.30),
        "primary",
    )
    assert all(r["import_price"] == pytest.approx(0.40) for r in rows[1:])
    assert all(r["import_price_source"] == "consolidated" for r in rows[1:])
    assert all(r["export_price"] == pytest.approx(0.05) for r in rows)


def test_the_live_switch_is_declared_off_and_reaches_the_config():
    # test_the_live_switch_is_declared_off_and_reaches_the_config
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    from custom_components.nimbus_load import const, sensor

    assert const.DEFAULT_SOLVER_PRICE_CONSOLIDATION_LIVE is False
    assert (
        const.CONF_SOLVER_PRICE_CONSOLIDATION_LIVE == "solver_price_consolidation_live"
    )
    assert (
        const.CONF_SOLVER_PRICE_CONSOLIDATION_LIVE in sensor._SOLVER_SWITCH_ENTITY_KEYS
    )

    # test_no_factor_or_no_aemo_point_is_none_never_a_guess
    late = [(T0 + timedelta(minutes=10), 0.10)]
    assert (
        pc.retail_from_rrp_series(GRID, late, {}, _bucket, lambda t: K)[:2]
        == [None] * 2
    )
    assert (
        pc.retail_from_rrp_series(GRID, late, {}, _bucket, lambda t: None) == [None] * 6
    )
    assert pc.retail_from_rrp_series(GRID, None, {}, _bucket, lambda t: K) == [None] * 6

    # test_by_contract_import_carries_gst_and_the_factor_is_reported
    w = _Writer({"sensor.tni": "NCMA", "sensor.dlf": "BL0A"})
    imp, exp, summary = prices._retail_from_rrp(
        w, CODES, T0, GRID, [(T0, 0.10)], [], [], {}
    )
    assert summary == {
        "tni": "NCMA",
        "dlf_code": "BL0A",
        "financial_year": "2026-27",
        "value": round(K, 6),
        "reason": "ok",
    }
    assert exp[0] == pytest.approx(0.08 * K)
    assert imp[:2] == [
        pytest.approx(0.08 * K * loss_factors.IMPORT_GST),
        pytest.approx(0.10 * K * loss_factors.IMPORT_GST),
    ]
    assert [s(T0) for s in w.scales] == [
        pytest.approx(K * loss_factors.IMPORT_GST),
        pytest.approx(K),
    ]

    # test_without_usable_codes_nothing_is_priced_by_contract_and_it_says_why
    plain = {"solver_regional_spot_current_price_sensor": "sensor.rrp_now"}
    assert (
        prices._retail_from_rrp(_Writer({}), plain, T0, GRID, [(T0, 0.1)], [], [], {})
        is None
    )
    w = _Writer({"sensor.tni": "ZZZZ", "sensor.dlf": "unavailable"})
    imp, exp, summary = prices._retail_from_rrp(
        w, CODES, T0, GRID, [(T0, 0.1)], [], [], {}
    )
    assert (imp, exp, summary["reason"]) == (None, None, "no_codes")
    w = _Writer({"sensor.tni": "ZZZZ", "sensor.dlf": "BL0A"})
    assert (
        prices._retail_from_rrp(w, CODES, T0, GRID, [], [], [], {})[2]["reason"]
        == "unknown_tni"
    )


# --- the AEMO source priced by contract ---------------------------------------------------------

T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)  # 20:00 NEM time
GRID = [T0 + timedelta(minutes=5 * i) for i in range(6)]
K = 0.9912 * 1.0594  # a site's MLF x DLF


def _bucket(t):
    return t.hour * 12 + t.minute // 5


class _Writer:
    def __init__(self, states):
        self._states, self.scales = states, []

    def ha_get(self, entity_id):
        return {"state": self._states[entity_id]}

    def compute_5min_offset(self, history, **kwargs):
        self.scales.append(kwargs["wholesale_scale"])
        return {}

    def price_now(self, entity_id, fallback=0.0, *, quiet=False):
        return 0.08


CODES = {
    "solver_loss_factor_tni_sensor": "sensor.tni",
    "solver_loss_factor_dlf_code_sensor": "sensor.dlf",
    "solver_regional_spot_current_price_sensor": "sensor.rrp_now",
}
