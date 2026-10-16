"""nimbus #1797 (PR D of the two-stage stochastic programme): signed
price-forecast error history by source, lead bucket and spike regime.

Covers the pure module (`solver_inputs/price_error_history.py`): recording,
bucketing, the stride, bounded memory, threshold re-filing, persistence, the
quantiles, the Beta(1, 1) spike p_hit and leave-one-day-out coverage. Then the
wiring: `solver_writer.apply_price_consolidation()` feeds it from the same
series and settled prices the #1786 tracker scores, persists it, publishes it
inside `price_consolidation`, and the real `main()` plans exactly as without it.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
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
from solver_inputs import price_error_history as peh

NOW = datetime(2026, 10, 16, 2, 0, 30, tzinfo=UTC)


def _key_on_stride(on: bool, near: datetime = NOW) -> datetime:
    """The start of the first 5-minute interval at or after `near` whose key
    is (or is not) the one kept normal pair of its half hour."""
    k = pc.interval_key(near) + 1
    while peh.on_stride(k) != on:
        k += 1
    return datetime.fromtimestamp(k * pc.INTERVAL_SECONDS, UTC)


def _grade(hist, side, source, interval, forecast, settled, lead_min=2.0):
    """Record one forecast `lead_min` ahead of `interval`, then settle it."""
    issued = interval - timedelta(minutes=lead_min)
    hist.record(side, source, issued, interval, forecast)
    hist.observe_settled(side, interval + timedelta(seconds=10), settled)
    return hist.observe_settled(side, interval + timedelta(minutes=5, seconds=10), None)


# -- buckets, stride, threshold --------------------------------------------------


@pytest.mark.parametrize(
    ("lead", "bucket"),
    [
        (0.0, "0-5 min"),
        (5.0, "5-60 min"),
        (60.0, "1-4 h"),
        (240.0, "4-24 h"),
        (1439.0, "4-24 h"),
        (1440.0, ">24 h"),
        (2879.0, ">24 h"),
        (2880.0, None),
        (-1.0, None),
    ],
)
def test_lead_bucket_extends_the_trackers_four_with_beyond_24h(lead, bucket):
    assert peh.lead_bucket(lead) == bucket
    # The first four are the tracker's own, unchanged.
    assert peh.LEAD_BUCKETS[:4] == pc.LEAD_BUCKETS


def test_on_stride_keeps_one_interval_per_half_hour_and_rotates():
    base = 6 * 1000
    positions = []
    for block in range(6):
        kept = [
            k for k in range(base + 6 * block, base + 6 * block + 6) if peh.on_stride(k)
        ]
        assert len(kept) == 1
        positions.append(kept[0] % 6)
    assert sorted(positions) == list(range(6))


def test_spike_threshold_reuses_existing_settings_only():
    # The household's own retail spike threshold wins when set.
    assert peh.spike_threshold({"solver_price_spike_threshold": 1.5}) == (
        1.5,
        "solver_price_spike_threshold",
    )
    # Else the P5MIN false-spike threshold, $/MWh -> $/kWh (default 5000).
    assert peh.spike_threshold({}) == (5.0, "solver_p5min_false_spike_threshold_mwh")
    # ...capped at 90% of the market price cap, as p5min_guard does.
    thr, _src = peh.spike_threshold({"solver_p5min_false_spike_threshold_mwh": 30000})
    assert thr == pytest.approx(0.9 * 23.2)
    # Both off: no regime split, no spike counts.
    assert peh.spike_threshold({"solver_p5min_false_spike_threshold_mwh": 0}) == (
        None,
        None,
    )


def test_classify():
    assert peh.classify(6.0, 0.3, 5.0) == peh.CLS_SPIKE
    assert peh.classify(0.3, 6.0, 5.0) == peh.CLS_SURPRISE
    assert peh.classify(0.3, 0.3, 5.0) == peh.CLS_NORMAL
    assert peh.classify(9.0, 9.0, None) == peh.CLS_NORMAL


# -- recording and settling ------------------------------------------------------


def test_record_keeps_forecast_closest_to_the_bucket_lead_including_beyond_24h():
    h = peh.PriceErrorHistory()
    target = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    # >24 h bucket, representative lead 36 h.
    h.record("import", "a", target - timedelta(hours=46), target, 1.0)
    h.record("import", "a", target - timedelta(hours=35), target, 2.0)
    h.record("import", "a", target - timedelta(hours=26), target, 3.0)
    slot = h.pending["import"][pc.interval_key(target)]["a"][">24 h"]
    assert slot[1] == 2.0
    # 48 h or more ahead is not kept at all.
    far = target + timedelta(hours=60)
    h.record("import", "a", target, far, 9.0)
    assert pc.interval_key(far) not in h.pending["import"]


def test_settled_pair_is_signed_and_carries_key_and_day():
    h = peh.PriceErrorHistory()
    h.set_threshold(5.0)
    t = _key_on_stride(True)
    assert _grade(h, "import", "primary", t, 0.30, 0.25) == 1
    (sample,) = h.samples["import"]["primary"]["0-5 min"][peh.CLS_NORMAL]
    key, day, forecast, settled = sample
    assert key == pc.interval_key(t)
    assert day == t.date().toordinal()
    assert (forecast, settled) == (0.30, 0.25)
    assert h.pending["import"] == {}


def test_off_stride_normal_pair_is_dropped_but_rare_events_are_always_kept():
    h = peh.PriceErrorHistory()
    h.set_threshold(5.0)
    off = _key_on_stride(False)
    assert _grade(h, "import", "primary", off, 0.30, 0.25) == 0
    off2 = _key_on_stride(False, off + timedelta(minutes=30))
    assert _grade(h, "import", "primary", off2, 6.0, 0.40) == 1  # false spike
    off3 = _key_on_stride(False, off2 + timedelta(minutes=30))
    assert _grade(h, "import", "primary", off3, 0.30, 7.0) == 1  # surprise
    classes = h.samples["import"]["primary"]["0-5 min"]
    assert peh.CLS_NORMAL not in classes
    assert len(classes[peh.CLS_SPIKE]) == 1
    assert len(classes[peh.CLS_SURPRISE]) == 1


def test_sides_settle_independently():
    h = peh.PriceErrorHistory()
    t = _key_on_stride(True)
    h.record("import", "primary", t - timedelta(minutes=2), t, 0.3)
    h.record("export", "primary", t - timedelta(minutes=2), t, 0.05)
    for side, settled in (("import", 0.31), ("export", 0.04)):
        h.observe_settled(side, t + timedelta(seconds=5), settled)
        h.observe_settled(side, t + timedelta(minutes=5, seconds=5), None)
    assert h.samples["import"]["primary"]["0-5 min"]["normal"][0][3] == 0.31
    assert h.samples["export"]["primary"]["0-5 min"]["normal"][0][3] == 0.04


def test_no_settled_price_grades_nothing():
    h = peh.PriceErrorHistory()
    t = _key_on_stride(True)
    h.record("import", "primary", t - timedelta(minutes=2), t, 0.3)
    h.observe_settled("import", t + timedelta(seconds=5), None)
    assert h.observe_settled("import", t + timedelta(minutes=5, seconds=5), None) == 0
    assert h.sample_count() == 0


# -- bounded memory -------------------------------------------------------------


def test_pending_is_bounded_to_48h_and_dropped_once_past():
    h = peh.PriceErrorHistory()
    grid = [NOW + timedelta(minutes=30 * i) for i in range(200)]  # 100 h
    h.record_series("import", "a", NOW, grid, [0.5] * len(grid), [1.0] * len(grid))
    assert max(h.pending["import"]) <= pc.interval_key(NOW + timedelta(hours=48))
    later = NOW + timedelta(hours=10)
    h.observe_settled("import", later, 1.0)
    assert min(h.pending["import"]) >= pc.interval_key(later)


def test_prune_keeps_days_and_caps_each_class():
    h = peh.PriceErrorHistory()
    today = NOW.date().toordinal()
    old = [[1, today - peh.KEEP_DAYS, 0.3, 0.3]]
    recent = [[2 + i, today, 0.3, 0.3] for i in range(peh.MAX_SAMPLES_PER_CELL + 50)]
    h.samples["import"]["a"] = {"0-5 min": {"normal": old + recent}}
    h.prune(NOW)
    kept = h.samples["import"]["a"]["0-5 min"]["normal"]
    assert len(kept) == peh.MAX_SAMPLES_PER_CELL
    assert all(s[1] == today for s in kept)
    assert kept[-1] == recent[-1]  # newest survive


def test_a_threshold_change_refiles_kept_pairs():
    h = peh.PriceErrorHistory()
    h.set_threshold(5.0)
    t = _key_on_stride(True)
    _grade(h, "import", "a", t, 3.0, 0.3)
    assert list(h.samples["import"]["a"]["0-5 min"]) == [peh.CLS_NORMAL]
    assert h.set_threshold(2.0) is True
    assert list(h.samples["import"]["a"]["0-5 min"]) == [peh.CLS_SPIKE]
    assert h.set_threshold(2.0) is False


# -- persistence ------------------------------------------------------------------


def test_round_trip():
    h = peh.PriceErrorHistory()
    h.set_threshold(5.0)
    t = _key_on_stride(True)
    _grade(h, "import", "primary", t, 0.30, 0.25)
    h.record("export", "aemo_markup", NOW, NOW + timedelta(hours=30), 0.1)
    h.observe_settled("export", NOW, 0.07)
    back = peh.PriceErrorHistory.from_dict(json.loads(json.dumps(h.to_dict())))
    assert back.to_dict() == h.to_dict()
    assert back.threshold == 5.0


@pytest.mark.parametrize(
    "bad",
    [None, [], "x", {"pending": {"import": {"k": {}}}}, {"samples": {"import": 3}}],
)
def test_malformed_state_loads_empty(bad):
    assert peh.PriceErrorHistory.from_dict(bad).sample_count() == 0


# -- the distribution ----------------------------------------------------------------


def _pairs(diffs, forecast=0.30, days=3):
    return [
        [i, 739000 + i % days, forecast, round(forecast + d, 6)]
        for i, d in enumerate(diffs)
    ]


def test_cell_stats_quantiles_of_difference_and_ratio():
    diffs = [(i - 50) / 1000 for i in range(101)]  # -0.05 .. +0.05
    row = dict(zip(peh.COLUMNS, peh.cell_stats(_pairs(diffs)), strict=True))
    assert row["n"] == 101 and row["days"] == 3
    assert row["d10"] == pytest.approx(-0.04)
    assert row["d50"] == pytest.approx(0.0)
    assert row["d90"] == pytest.approx(0.04)
    assert row["r_n"] == 101
    assert row["r50"] == pytest.approx(1.0)
    assert row["r10"] == pytest.approx((0.30 - 0.04) / 0.30, abs=1e-3)
    assert row["ratio_ok"] == 1
    # Leave-one-day-out band coverage, roughly 10% each side.
    assert 5.0 <= row["d_below_pct"] <= 15.0
    assert 5.0 <= row["d_above_pct"] <= 15.0


def test_ratio_is_flagged_where_forecasts_sit_near_zero():
    near_zero = _pairs([0.01] * 40, forecast=0.002)
    fine = _pairs([0.01] * 40, forecast=0.30)
    row = dict(zip(peh.COLUMNS, peh.cell_stats(near_zero + fine), strict=True))
    assert row["n"] == 80 and row["r_n"] == 40
    assert row["d50"] == pytest.approx(0.01)  # difference is always defined
    assert row["r50"] is not None
    assert row["ratio_ok"] == 0
    negative = dict(
        zip(
            peh.COLUMNS, peh.cell_stats(_pairs([0.0] * 40, forecast=-0.05)), strict=True
        )
    )
    assert (
        negative["r_n"] == 0 and negative["r50"] is None and negative["ratio_ok"] == 0
    )


def test_cell_stats_below_min_samples_publishes_counts_only():
    row = peh.cell_stats(_pairs([0.0] * (peh.MIN_SAMPLES - 1)))
    assert row[0] == peh.MIN_SAMPLES - 1
    assert row[2:7] == [None] * 5
    assert row[8:13] == [None] * 5


def test_spike_p_hit_has_a_beta_1_1_prior():
    thr = 5.0
    assert peh.spike_stats([], [], thr) == [0, 0, 0, 0.5]
    hits = [[0, 0, 6.0, 8.0]] * 3
    misses = [[0, 0, 6.0, 0.4]]
    surprises = [[0, 0, 0.3, 9.0]] * 2
    assert peh.spike_stats(hits + misses, surprises, thr) == [
        3,
        1,
        2,
        pytest.approx(4 / 6, abs=1e-4),
    ]


def test_loo_band_coverage():
    assert peh.loo_band_coverage([1] * 10, list(range(10))) == (None, None)
    # Day 2 sits wholly above days 1's band: every value graded "above".
    days = [1] * 20 + [2] * 5
    values = [float(i % 10) for i in range(20)] + [100.0] * 5
    below, above = peh.loo_band_coverage(days, values)
    assert above >= 100.0 * 5 / 25
    assert below is not None


def test_summarise_below_regime_uses_on_stride_pairs_only():
    h = peh.PriceErrorHistory()
    h.set_threshold(5.0)
    on = [k for k in range(6000, 6600) if peh.on_stride(k)]
    off = [k for k in range(6000, 6600) if not peh.on_stride(k)]
    cell = {
        peh.CLS_NORMAL: [[k, 739000 + k % 3, 0.3, 0.3] for k in on],
        peh.CLS_SURPRISE: [[k, 739000, 0.3, 6.0] for k in on[:2] + off[:10]],
        peh.CLS_SPIKE: [[k, 739000, 6.0, 7.0] for k in off[:3]],
    }
    h.samples["import"]["primary"] = {"1-4 h": cell}
    out = peh.summarise(h)["import"]["primary"]["1-4 h"]
    assert out["below"][0] == len(on) + 2  # 10 off-stride surprises excluded
    assert out["at_or_above"][0] == 3
    # ...but every surprise counts towards the spike statistics.
    assert out["spike"] == [3, 0, 12, pytest.approx(4 / 5, abs=1e-4)]


def test_summary_without_a_threshold_has_no_spike_counts():
    h = peh.PriceErrorHistory()
    assert peh.on_stride(6004)
    h.samples["import"]["a"] = {"0-5 min": {"normal": [[6004, 1, 0.3, 0.3]]}}
    out = peh.summarise(h)["import"]["a"]["0-5 min"]
    assert "spike" not in out and "at_or_above" not in out


def _full_history(sources=("primary", "secondary_2", "secondary_3", "aemo_markup")):
    h = peh.PriceErrorHistory()
    h.set_threshold(5.0)
    keys = [k for k in range(6_000_000, 6_000_000 + 6 * 1008) if peh.on_stride(k)]
    for side in peh.SIDES:
        for src in sources:
            for bucket, *_ in peh.LEAD_BUCKETS:
                h.samples[side].setdefault(src, {})[bucket] = {
                    "normal": [
                        [k, 739000 + i // 48, 0.3, 0.3 + ((i * 37) % 101 - 50) / 1000]
                        for i, k in enumerate(keys)
                    ],
                    "spike": [
                        [k, 739000 + i % 21, 6.0, 7.0] for i, k in enumerate(keys[:40])
                    ],
                    "surprise": [[k, 739000, 0.3, 6.0] for k in keys[:5]],
                }
    return h


def test_compact_attribute_stays_small_at_a_full_three_weeks():
    """Every source, both sides, all five buckets, KEEP_DAYS of pairs: the
    published attribute stays under 3 KB (the battery-forecast sensor's
    non-series attributes measure ~7 KB in the golden scenarios, against the
    recorder's 16 KB cap)."""
    h = _full_history()
    out = peh.compact(peh.summarise(h))
    assert len(json.dumps(out)) < 3000
    row = dict(
        zip(peh.COMPACT_COLUMNS, out["import"]["primary"]["4-24 h"], strict=True)
    )
    assert row["n"] == 1008 + 5  # normal pairs plus on-stride surprises
    assert row["hits"] == 40 and row["misses"] == 0 and row["surprises"] == 5
    assert row["p_hit"] == pytest.approx(41 / 42, abs=1e-4)
    assert row["d10"] < 0 < row["d90"]


def test_summary_is_cached_until_the_samples_change():
    h = _full_history(("primary",))
    first = peh._summary(h)
    assert peh._summary(h) is first
    h.set_threshold(4.0)
    assert peh._summary(h) is not first


# -- wiring: apply_price_consolidation() ------------------------------------------


_PRICES = SimpleNamespace(
    import_real_mask=[True] * 4,
    export_real_mask=[True] * 4,
    aemo_import_series=None,
    aemo_export_series=None,
)


def _cycle(now, settled_import=0.31, cfg=None):
    grid = [now] + [
        datetime.fromtimestamp((pc.interval_key(now) + 1 + i) * 300, UTC)
        for i in range(3)
    ]
    return solver_writer.apply_price_consolidation(
        cfg or {},
        now,
        grid,
        [5 / 60] * 4,
        1,
        _PRICES,
        [0.30] * 4,
        [0.05] * 4,
        settled_import,
        0.05,
        [0.30] * 4,
        [0.05] * 4,
        ["primary"] * 4,
        ["primary"] * 4,
    )


def test_fed_every_cycle_from_the_trackers_own_settle_point_and_persisted():
    start = _key_on_stride(True) - timedelta(minutes=5) + timedelta(seconds=10)
    with isolated_state_paths(solver_writer) as tmp:
        # An hour of 5-minute cycles: saved on the first (the threshold is
        # adopted), then on the first grade 30 minutes or more later.
        for i in range(13):
            result = _cycle(start + timedelta(minutes=5 * i))
        eh = result["error_history"]
        assert eh["read_by_dispatch"] is False
        assert eh["threshold"] == 5.0
        assert eh["samples"] >= 1
        # The 0-5 min forecast of 0.30 settled at 0.31: a +0.01 signed error.
        hist = solver_writer._PRICE_ERROR_HISTORY_CACHE
        pairs = hist.samples["import"]["primary"]["0-5 min"]["normal"]
        assert pairs[0][2:] == [0.3, 0.31]
        # The #1786 tracker scored the very same intervals.
        tracker = solver_writer._PRICE_CONSOLIDATION_CACHE["import"]
        assert tracker.samples["primary"]["0-5 min"] >= len(pairs)
        # Persisted in the config dir, and restored after a restart.
        path = os.path.join(tmp, "nimbus_solver_price_error_history.json")
        assert os.path.exists(path)
        solver_writer._PRICE_ERROR_HISTORY_CACHE = None
        restored = peh._load(solver_writer)
        saved = restored.samples["import"]["primary"]["0-5 min"]["normal"]
        assert saved and saved == pairs[: len(saved)]
        assert restored.threshold == 5.0
        # The file also carries the full table; the attribute, the compact one.
        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["summary"]["columns"] == list(peh.COLUMNS)
        assert "below" in on_disk["summary"]["import"]["primary"]["0-5 min"]
        row = eh["import"]["primary"]["0-5 min"]
        assert len(row) == len(peh.COMPACT_COLUMNS)


def test_saves_at_most_every_save_interval():
    start = _key_on_stride(True) - timedelta(minutes=5) + timedelta(seconds=10)
    with (
        isolated_state_paths(solver_writer),
        patch.object(peh, "_save", wraps=peh._save) as save,
    ):
        for i in range(8):  # 40 minutes of 5-minute cycles
            _cycle(start + timedelta(minutes=5 * i))
    assert 1 <= save.call_count <= 2


def test_a_failure_in_the_history_never_breaks_consolidation():
    with (
        isolated_state_paths(solver_writer),
        patch.object(peh, "summarise", side_effect=RuntimeError("boom")),
    ):
        result = _cycle(NOW)
    assert result["import"]["sources"] == ["primary"]
    assert result["error_history"] == {"read_by_dispatch": False, "error": "boom"}


def test_a_persistent_failure_warns_once_not_every_cycle(caplog, monkeypatch):
    """A failure that persists would otherwise log a WARNING every 15 s."""
    monkeypatch.setattr(peh, "_WARNED", set())
    with (
        isolated_state_paths(solver_writer),
        patch.object(peh, "summarise", side_effect=RuntimeError("boom")),
    ):
        for i in range(3):
            _cycle(NOW + timedelta(minutes=5 * i))
    assert caplog.text.count("price error history failed: boom") == 1


def test_an_unwritable_state_file_is_reported_once(caplog, monkeypatch, tmp_path):
    """The cron copy's default path is under a root-owned /opt, where a first
    write fails until the file is pre-created. That used to be swallowed by
    `except OSError: pass`, so the history silently never persisted."""
    monkeypatch.setattr(peh, "_WARNED", set())
    sw = SimpleNamespace(PRICE_ERROR_HISTORY_PATH=str(tmp_path / "missing" / "x.json"))
    hist = peh.PriceErrorHistory.from_dict(None)
    peh._save(sw, hist)
    peh._save(sw, hist)
    assert caplog.text.count("price error history not saved") == 1


def test_a_stale_source_settled_price_is_not_graded():
    """apply_price_consolidation() is handed None when the source is on its
    AEMO fallback (#1793); nothing is graded, exactly as for the tracker."""
    start = _key_on_stride(True) - timedelta(minutes=5) + timedelta(seconds=10)
    with isolated_state_paths(solver_writer):
        for i in range(4):
            result = _cycle(start + timedelta(minutes=5 * i), settled_import=None)
    assert "primary" not in result["error_history"]["import"]


# -- the real main() -------------------------------------------------------------


def _run_main(history_disabled: bool = False):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    get = guardrail._make_ha_get()

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
    if history_disabled:
        patches.append(patch.object(peh, "update", return_value={}))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        solver_writer.main()
    return posted["sensor.nimbus_solver_battery_forecast"][1]


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_publishes_it_and_plans_exactly_as_without_it(freezer):
    attrs = _run_main()
    without = _run_main(history_disabled=True)
    assert attrs["forecast"] == without["forecast"]
    assert attrs["total_cost"] == without["total_cost"]
    eh = attrs["price_consolidation"]["error_history"]
    assert eh["read_by_dispatch"] is False
    assert eh["columns"] == list(peh.COMPACT_COLUMNS)
