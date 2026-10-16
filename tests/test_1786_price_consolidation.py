"""nimbus #1786 (Mark Purcell): consolidated price forecast and per-source
running error, in shadow by default and live behind a switch.

Covers the pure module (`solver_inputs/price_consolidation.py`), the
orchestration in `solver_writer.apply_price_consolidation()`, and the real
`main()`: with the live switch off the scheduling prices are byte-identical to
a run without consolidation at all; with it on, only future periods change and
the current settled block keeps its settled price.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import pytest
import solver_shared
import solver_writer
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths
from solver_inputs import price_consolidation as pc

NOW = datetime(2026, 10, 16, 2, 0, 30, tzinfo=UTC)


# -- consolidate() -------------------------------------------------------


def test_median_of_available_sources():
    values, spread, method = pc.consolidate(
        {"a": [1.0, 1.0], "b": [3.0, 2.0], "c": [10.0, None]}
    )
    assert values == [3.0, 1.5]
    assert spread == [9.0, 1.0]
    assert method == "median"


def test_weighted_mean_when_weights_cover_every_available_source():
    values, spread, method = pc.consolidate(
        {"a": [1.0], "b": [3.0]}, {"a": 0.75, "b": 0.25}
    )
    assert values == [pytest.approx(1.5)]
    assert spread == [2.0]
    assert method == "weighted"


def test_weights_missing_a_source_fall_back_to_median():
    values, _spread, method = pc.consolidate(
        {"a": [1.0], "b": [3.0], "c": [5.0]}, {"a": 0.5, "b": 0.5}
    )
    assert values == [3.0]
    assert method == "median"


def test_per_period_weights_and_mixed_method():
    values, _spread, method = pc.consolidate(
        {"a": [1.0, 1.0], "b": [3.0, 3.0]}, [{"a": 1.0, "b": 1.0}, None]
    )
    assert values == [2.0, 2.0]
    assert method == "mixed"


def test_period_with_no_source_is_none():
    values, spread, method = pc.consolidate({"a": [None, 2.0], "b": [None]})
    assert values == [None, 2.0]
    assert spread == [None, 0.0]
    assert method == "median"
    assert pc.consolidate({"a": [None]})[2] == "none"


def test_aemo_markup_series():
    t0 = NOW
    pts = [(t0 + timedelta(minutes=30), 0.10), (t0 + timedelta(minutes=60), 0.20)]
    grid = [t0, t0 + timedelta(minutes=30), t0 + timedelta(minutes=90)]
    out = pc.aemo_markup_series(grid, pts, {1: 0.05}, lambda t: 1)
    assert out == [None, pytest.approx(0.15), pytest.approx(0.25)]
    assert pc.aemo_markup_series(grid, [], {}, lambda t: 0) == [None] * 3


# -- lead buckets ------------------------------------------------------------


@pytest.mark.parametrize(
    ("lead", "bucket"),
    [
        (0.0, "0-5 min"),
        (4.9, "0-5 min"),
        (5.0, "5-60 min"),
        (59.9, "5-60 min"),
        (60.0, "1-4 h"),
        (239.0, "1-4 h"),
        (240.0, "4-24 h"),
        (1439.0, "4-24 h"),
        (1440.0, None),
        (-1.0, None),
    ],
)
def test_lead_bucket(lead, bucket):
    assert pc.lead_bucket(lead) == bucket


def test_record_keeps_forecast_closest_to_the_bucket_lead():
    tr = pc.SourceErrorTracker()
    target = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    # 5-60 min bucket, representative lead 32.5 min.
    tr.record("a", target - timedelta(minutes=55), target, 1.0)
    tr.record("a", target - timedelta(minutes=33), target, 2.0)
    tr.record("a", target - timedelta(minutes=10), target, 3.0)
    slot = tr.pending[pc.interval_key(target)]["a"]["5-60 min"]
    assert slot[1] == 2.0
    # 0-5 min bucket gets its own forecast.
    tr.record("a", target - timedelta(minutes=2), target, 4.0)
    assert tr.pending[pc.interval_key(target)]["a"]["0-5 min"][1] == 4.0
    # Beyond 24 h is no bucket; nothing recorded.
    far = target + timedelta(hours=30)
    tr.record("a", target, far, 9.0)
    assert pc.interval_key(far) not in tr.pending


def test_record_series_expands_a_30_minute_period_to_six_intervals():
    tr = pc.SourceErrorTracker()
    start = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
    tr.record_series("a", NOW, [start], [0.5], [1.0])
    assert len(tr.pending) == 6


# -- EWMA and settling ---------------------------------------------------------


def test_settle_updates_ewma_mae_and_prunes():
    tr = pc.SourceErrorTracker()
    t = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    tr.record("a", t - timedelta(minutes=2), t, 1.0)
    tr.settle(pc.interval_key(t), 1.5)
    assert tr.mae["a"]["0-5 min"] == pytest.approx(0.5)
    assert tr.samples["a"]["0-5 min"] == 1
    assert tr.pending == {}
    t2 = t + timedelta(minutes=5)
    tr.record("a", t2 - timedelta(minutes=2), t2, 1.0)
    tr.settle(pc.interval_key(t2), 1.0)
    assert tr.mae["a"]["0-5 min"] == pytest.approx(0.95 * 0.5)
    assert tr.samples["a"]["0-5 min"] == 2


def test_observe_settled_scores_the_previous_interval_once():
    tr = pc.SourceErrorTracker()
    t1 = datetime(2026, 10, 16, 3, 0, 10, tzinfo=UTC)
    nxt = datetime(2026, 10, 16, 3, 5, tzinfo=UTC)
    tr.record("a", t1, nxt, 1.0)  # 4.8 min ahead
    assert tr.observe_settled(t1, 0.9) is False
    t2 = nxt + timedelta(seconds=20)
    assert tr.observe_settled(t2, 1.2) is False  # first sight of nxt
    assert tr.observe_settled(t2 + timedelta(seconds=15), 1.4) is False
    # Next interval: nxt is scored with its LAST settled value, 1.4.
    assert tr.observe_settled(t2 + timedelta(minutes=5), 2.0) is True
    assert tr.mae["a"]["0-5 min"] == pytest.approx(0.4)
    assert tr.samples["a"]["0-5 min"] == 1


# -- weights -------------------------------------------------------------


def _tracker_with(samples_a: int, samples_b: int) -> pc.SourceErrorTracker:
    tr = pc.SourceErrorTracker()
    tr.mae = {"a": {"0-5 min": 0.01}, "b": {"0-5 min": 0.03}}
    tr.samples = {"a": {"0-5 min": samples_a}, "b": {"0-5 min": samples_b}}
    return tr


def test_weights_need_min_samples_from_every_source():
    assert _tracker_with(50, 49).weights("0-5 min", ["a", "b"]) is None
    w = _tracker_with(50, 50).weights("0-5 min", ["a", "b"])
    assert w == {"a": pytest.approx(0.75), "b": pytest.approx(0.25)}
    assert sum(w.values()) == pytest.approx(1.0)
    assert _tracker_with(50, 50).weights("1-4 h", ["a", "b"]) is None


def test_round_trip():
    tr = _tracker_with(60, 70)
    t = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    tr.record("a", t - timedelta(minutes=40), t, 0.25)
    tr.last_interval = 123
    tr.last_settled = 0.31
    back = pc.SourceErrorTracker.from_dict(tr.to_dict())
    assert back.to_dict() == tr.to_dict()
    assert pc.SourceErrorTracker.from_dict({"mae": "junk"}).to_dict() == (
        pc.SourceErrorTracker().to_dict()
    )
    assert pc.SourceErrorTracker.from_dict(None).mae == {}


def test_pending_is_bounded():
    tr = pc.SourceErrorTracker()
    grid = [NOW + timedelta(minutes=30 * i) for i in range(200)]  # 100 h
    tr.record_series("a", NOW, grid, [0.5] * len(grid), [1.0] * len(grid))
    newest = max(tr.pending)
    assert newest <= pc.interval_key(NOW + timedelta(hours=24))
    tr.observe_settled(NOW + timedelta(hours=10), 1.0)
    assert min(tr.pending) >= pc.interval_key(NOW + timedelta(hours=10))


# -- apply_price_consolidation() -------------------------------------------


def _apply(live: bool, aemo=None, n_settled=2):
    grid = [NOW + timedelta(minutes=i) for i in range(n_settled)] + [
        NOW + timedelta(minutes=5 * (i + 1)) for i in range(3)
    ]
    hours = [1 / 60] * n_settled + [5 / 60] * 3
    n = len(grid)
    imp = [0.30] * n
    exp = [0.05] * n
    prices = SimpleNamespace(
        import_real_mask=[True] * n,
        export_real_mask=[True] * n,
        aemo_import_series=aemo,
        aemo_export_series=None,
    )
    raw, exp_sched = list(imp), list(exp)
    lab_i, lab_e = ["primary"] * n, ["primary"] * n
    cfg = {"solver_price_consolidation_live": live}
    with isolated_state_paths(solver_writer):
        result = solver_writer.apply_price_consolidation(
            cfg,
            NOW,
            grid,
            hours,
            n_settled,
            prices,
            list(imp),
            list(exp),
            0.30,
            0.05,
            raw,
            exp_sched,
            lab_i,
            lab_e,
        )
    return result, raw, exp_sched, lab_i, lab_e


def test_off_changes_nothing_and_publishes_shadow():
    aemo = [0.90] * 5
    result, raw, exp, lab_i, lab_e = _apply(False, aemo=aemo)
    assert raw == [0.30] * 5 and exp == [0.05] * 5
    assert lab_i == ["primary"] * 5 and lab_e == ["primary"] * 5
    assert result["mode"] == "shadow"
    assert result["import"]["sources"] == ["aemo_markup", "primary"]
    assert result["consolidated_import_price"] == [pytest.approx(0.60)] * 5
    assert result["import"]["spread_now"] == pytest.approx(0.60)


def test_on_replaces_only_future_periods():
    aemo = [0.90, 0.90, 0.90, None, 0.90]
    result, raw, exp, lab_i, _lab_e = _apply(True, aemo=aemo)
    assert result["mode"] == "live"
    # The settled block (indices 0, 1) keeps its settled price.
    assert raw[:2] == [0.30, 0.30] and lab_i[:2] == ["primary", "primary"]
    # Future periods take the consolidated median of primary and AEMO...
    assert raw[2] == pytest.approx(0.60) and lab_i[2] == "consolidated"
    # ...including where only the primary exists (median of one).
    assert raw[3] == pytest.approx(0.30) and lab_i[3] == "consolidated"
    assert raw[4] == pytest.approx(0.60)
    # Export has only the primary, so its values are unchanged.
    assert exp == [0.05] * 5


def test_on_leaves_a_period_with_no_consolidated_value():
    grid = [NOW, NOW + timedelta(minutes=5)]
    prices = SimpleNamespace(
        import_real_mask=[True, False],
        export_real_mask=[True, True],
        aemo_import_series=None,
        aemo_export_series=None,
    )
    raw, lab = [0.30, 0.77], ["primary", "fallback"]
    with isolated_state_paths(solver_writer):
        solver_writer.apply_price_consolidation(
            {"solver_price_consolidation_live": True},
            NOW,
            grid,
            [1 / 60, 5 / 60],
            1,
            prices,
            [0.30, 0.77],
            [0.05, 0.05],
            0.30,
            0.05,
            raw,
            [0.05, 0.05],
            lab,
            ["primary", "primary"],
        )
    assert raw == [0.30, 0.77] and lab == ["primary", "fallback"]


def test_failure_returns_none_and_modifies_nothing():
    raw = [0.30, 0.30]
    with (
        isolated_state_paths(solver_writer),
        patch.object(pc, "consolidate", side_effect=RuntimeError("boom")),
    ):
        out = solver_writer.apply_price_consolidation(
            {"solver_price_consolidation_live": True},
            NOW,
            [NOW, NOW + timedelta(minutes=5)],
            [1 / 60, 5 / 60],
            1,
            SimpleNamespace(),
            [0.30, 0.30],
            [0.05, 0.05],
            0.30,
            0.05,
            raw,
            [0.05, 0.05],
            ["primary"] * 2,
            ["primary"] * 2,
        )
    assert out is None and raw == [0.30, 0.30]


def test_trackers_persist_across_a_restart():
    grid = [NOW + timedelta(minutes=5 * (i + 1)) for i in range(3)]
    prices = SimpleNamespace(
        import_real_mask=[True] * 3,
        export_real_mask=[True] * 3,
        aemo_import_series=None,
        aemo_export_series=None,
    )

    def run(now):
        solver_writer.apply_price_consolidation(
            {},
            now,
            grid,
            [5 / 60] * 3,
            0,
            prices,
            [0.30] * 3,
            [0.05] * 3,
            0.31,
            0.05,
            [0.30] * 3,
            [0.05] * 3,
            ["primary"] * 3,
            ["primary"] * 3,
        )

    with isolated_state_paths(solver_writer):
        run(NOW)
        run(grid[0] + timedelta(seconds=10))
        run(grid[1] + timedelta(seconds=10))  # scores grid[0]'s interval, saves
        solver_writer._PRICE_CONSOLIDATION_CACHE = None  # a restart
        trackers = solver_writer._load_price_trackers()
        assert trackers["import"].samples["primary"]["0-5 min"] == 1
        assert trackers["import"].mae["primary"]["0-5 min"] == pytest.approx(0.01)


# -- the real main() -------------------------------------------------------


_SECONDARY = "sensor.fake_import_price_2"


def _ha_get_with_secondary(price: float):
    base = guardrail._make_ha_get()
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    forecast = [
        {"time": (start + timedelta(minutes=30 * i)).isoformat(), "value": price}
        for i in range(200)
    ]
    secondary = {"state": str(price), "attributes": {"forecast": forecast}}

    def _get(entity_id):
        if entity_id == _SECONDARY:
            return secondary
        return base(entity_id)

    return _get


def _run_main(cfg_extra: dict, consolidation_disabled: bool = False):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    attrs.update(cfg_extra)
    get = _ha_get_with_secondary(0.50)

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        return get(entity_id)

    posted: dict = {}

    def _post(entity_id, state, attrs=None):
        posted[entity_id] = (state, attrs)

    patches = [
        patch.object(solver_writer, "ha_get", side_effect=ha_get),
        patch.object(solver_shared, "ha_get", side_effect=ha_get),
        patch.object(solver_writer, "ha_post_state", side_effect=_post),
        isolated_state_paths(solver_writer),
    ]
    if consolidation_disabled:
        patches.append(
            patch.object(solver_writer, "apply_price_consolidation", return_value=None)
        )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        solver_writer.main()
    return posted["sensor.nimbus_solver_battery_forecast"][1]


_SCHEDULING_KEYS = (
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


def _scheduling(attrs):
    return [{k: row.get(k) for k in _SCHEDULING_KEYS} for row in attrs["forecast"]]


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_shadow_is_byte_identical_to_no_consolidation(freezer):
    cfg = {"solver_import_price_sensor_2": _SECONDARY}
    shadow = _run_main(cfg)
    without = _run_main(cfg, consolidation_disabled=True)
    assert _scheduling(shadow) == _scheduling(without)
    assert shadow["total_cost"] == without["total_cost"]
    assert shadow["price_consolidation"]["mode"] == "shadow"
    assert shadow["price_consolidation"]["import"]["sources"] == [
        "primary",
        "secondary_2",
    ]
    # Primary 0.30 and secondary 0.50: the shadow median is 0.40, published
    # on every row while the plan keeps scheduling on the primary.
    rows = shadow["forecast"]
    assert rows[1]["consolidated_import_price"] == pytest.approx(0.40)
    assert rows[1]["import_price"] == pytest.approx(0.30)
    assert without["price_consolidation"] is None
    assert rows[0]["consolidated_export_price"] == pytest.approx(0.05)


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_live_replaces_future_periods_only(freezer):
    cfg = {
        "solver_import_price_sensor_2": _SECONDARY,
        "solver_price_consolidation_live": True,
    }
    live = _run_main(cfg)
    rows = live["forecast"]
    assert live["price_consolidation"]["mode"] == "live"
    # 10:00 sits on a 5-minute mark, so the settled block is row 0 alone.
    assert rows[0]["import_price"] == pytest.approx(0.30)
    assert rows[0]["import_price_source"] == "primary"
    assert all(r["import_price"] == pytest.approx(0.40) for r in rows[1:])
    assert all(r["import_price_source"] == "consolidated" for r in rows[1:])
    # Export has one source, so its consolidated price is the primary's own.
    assert all(r["export_price"] == pytest.approx(0.05) for r in rows)


def test_switch_is_declared_off_and_bridged_to_cfg():
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    from custom_components.nimbus_load import const, sensor

    assert const.CONF_SOLVER_PRICE_CONSOLIDATION_LIVE == (
        "solver_price_consolidation_live"
    )
    assert const.DEFAULT_SOLVER_PRICE_CONSOLIDATION_LIVE is False
    assert const.CONF_SOLVER_PRICE_CONSOLIDATION_LIVE in (
        sensor._SOLVER_SWITCH_ENTITY_KEYS
    )
