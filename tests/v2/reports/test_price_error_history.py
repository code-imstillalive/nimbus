"""Each price source's signed forecast error, by lead bucket and spike
regime, kept so a later stochastic plan can read it (#1797) (#1837).

Pairs are recorded from the same series and settled prices the #1786
tracker scores: one normal pair per half hour (rotating), every spike and
surprise kept, three weeks bounded, re-filed when the threshold changes, and
persisted. The summary gives quantiles of difference and ratio, a Beta(1, 1)
spike hit rate and leave-one-day-out band coverage, compact enough for an
attribute. The plan never reads it yet: `main()` plans exactly as without
it. Synthetic values only.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import solver_shared
import solver_writer
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths
from solver_inputs import price_consolidation as pc
from solver_inputs import price_error_history as peh
from solver_inputs import price_offset_replay as por

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


def test_lead_bucket_extends_the_trackers_four_with_beyond_24h(
    caplog, monkeypatch, tmp_path
):
    # test_lead_bucket_extends_the_trackers_four_with_beyond_24h
    # test_lead_bucket_extends_the_trackers_four_with_beyond_24h
    for lead, bucket in [
        (0.0, "0-5 min"),
        (5.0, "5-60 min"),
        (60.0, "1-4 h"),
        (240.0, "4-24 h"),
        (1439.0, "4-24 h"),
        (1440.0, ">24 h"),
        (2879.0, ">24 h"),
        (2880.0, None),
        (-1.0, None),
    ]:
        assert peh.lead_bucket(lead) == bucket
        # The first four are the tracker's own, unchanged.
        assert peh.LEAD_BUCKETS[:4] == pc.LEAD_BUCKETS

    # test_spike_threshold_reuses_existing_settings_only
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

    # test_classify
    assert peh.classify(6.0, 0.3, 5.0) == peh.CLS_SPIKE
    assert peh.classify(0.3, 6.0, 5.0) == peh.CLS_SURPRISE
    assert peh.classify(0.3, 0.3, 5.0) == peh.CLS_NORMAL
    assert peh.classify(9.0, 9.0, None) == peh.CLS_NORMAL

    # test_malformed_state_loads_empty
    for bad in [
        None,
        [],
        "x",
        {"pending": {"import": {"k": {}}}},
        {"samples": {"import": 3}},
    ]:
        assert peh.PriceErrorHistory.from_dict(bad).sample_count() == 0

    # test_the_offset_replay_scores_fairly_and_finds_a_standing_premium (#1786,
    # Mark Purcell): the tracker scores a source with the offset it learned by
    # settle time; the blend only ever had the one known at forecast time.
    keys = [k for k in range(6_000_000, 6_010_000) if peh.on_stride(k)]

    def _cells(errors):
        return {"normal": [[k, 0, 0.10 + e, 0.10] for k, e in zip(keys, errors)]}

    steady = por.replay_source(por._pairs(_cells([0.02] * 200)), "1-4 h", 0.001)
    assert steady["offset_p10"] == pytest.approx(0.02)
    assert steady["fair_mae"] < 0.004  # only the warm-up pairs miss
    swing = por._pairs(_cells(([0.03] * 10 + [-0.03] * 10) * 10))
    today = por.replay_source(swing, "4-24 h", 0.05)
    assert today["hindsight_mae"] < today["fair_mae"]  # hindsight flatters it
    long_memory = por.replay_source(swing, "4-24 h", 0.001)
    assert (long_memory["offset_p90"] - long_memory["offset_p10"]) < (
        today["offset_p90"] - today["offset_p10"]
    )
    out = por.replay(
        {
            "import": {
                "a": {"1-4 h": _cells([0.02] * 60)},
                "b": {"1-4 h": _cells([0.0] * 60)},
            }
        }
    )
    assert out["import"]["blend"]["1-4 h"]["0.001"]["fair_mae"] < 0.01
    assert out["export"] == {"blend": {}}

    # test_loo_band_coverage
    assert peh.loo_band_coverage([1] * 10, list(range(10))) == (None, None)
    # Day 2 sits wholly above days 1's band: every value graded "above".
    days = [1] * 20 + [2] * 5
    values = [float(i % 10) for i in range(20)] + [100.0] * 5
    below, above = peh.loo_band_coverage(days, values)
    assert above >= 100.0 * 5 / 25
    assert below is not None

    # test_summary_without_a_threshold_has_no_spike_counts
    h = peh.PriceErrorHistory()
    assert peh.on_stride(6004)
    h.samples["import"]["a"] = {"0-5 min": {"normal": [[6004, 1, 0.3, 0.3]]}}
    out = peh.summarise(h)["import"]["a"]["0-5 min"]
    assert "spike" not in out and "at_or_above" not in out

    # test_summary_is_cached_until_the_samples_change
    h = _full_history(("primary",))
    first = peh._summary(h)
    assert peh._summary(h) is first
    h.set_threshold(4.0)
    assert peh._summary(h) is not first

    # test_fed_every_cycle_from_the_trackers_own_settle_point_and_persisted
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
        # #1786: the consolidated forecast is scored beside its sources, on
        # the same intervals, so its error can be read against the primary's.
        consolidated = hist.samples["import"]["consolidated"]["0-5 min"]["normal"]
        assert [p[:2] for p in consolidated] == [p[:2] for p in pairs]
        assert "consolidated" in eh["import"]
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

    # test_a_persistent_failure_warns_once_not_every_cycle: A failure that persists would otherwise log a WARNING every 15 s.
    caplog.clear()
    with monkeypatch.context() as mp:
        mp.setattr(peh, "_WARNED", set())
        with (
            isolated_state_paths(solver_writer),
            patch.object(peh, "summarise", side_effect=RuntimeError("boom")),
        ):
            for i in range(3):
                _cycle(NOW + timedelta(minutes=5 * i))
        assert caplog.text.count("price error history failed: boom") == 1

    # test_an_unwritable_state_file_is_reported_once: The cron copy's default path is under a root-owned /opt, where a first
    caplog.clear()
    tmp_2 = tmp_path / "test_an_unwritable_state_file_is_reported_once"
    tmp_2.mkdir()
    with monkeypatch.context() as mp:
        mp.setattr(peh, "_WARNED", set())
        sw = SimpleNamespace(PRICE_ERROR_HISTORY_PATH=str(tmp_2 / "missing" / "x.json"))
        hist = peh.PriceErrorHistory.from_dict(None)
        peh._save(sw, hist)
        peh._save(sw, hist)
        assert caplog.text.count("price error history not saved") == 1

    # test_a_stale_source_settled_price_is_not_graded: apply_price_consolidation() is handed None when the source is on its
    start = _key_on_stride(True) - timedelta(minutes=5) + timedelta(seconds=10)
    with isolated_state_paths(solver_writer):
        for i in range(4):
            result = _cycle(start + timedelta(minutes=5 * i), settled_import=None)
    assert "primary" not in result["error_history"]["import"]


# -- recording and settling ------------------------------------------------------


# -- bounded memory -------------------------------------------------------------


# -- persistence ------------------------------------------------------------------


# -- the distribution ----------------------------------------------------------------


def _pairs(diffs, forecast=0.30, days=3):
    return [
        [i, 739000 + i % days, forecast, round(forecast + d, 6)]
        for i, d in enumerate(diffs)
    ]


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
