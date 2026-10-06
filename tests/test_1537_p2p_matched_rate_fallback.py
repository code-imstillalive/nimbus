"""nimbus #1537 item 3: `{time, value}` matched-rate feeds, and a second
matched-rate source, with each interval's own validity and coverage kept.

Found live by Mark Purcell, 6 Oct 2026: LocalVolts v2's Current Sell Rate
(raw triple) read proportionP2P 0 / matchedCost 0 on all 287 forecast rows
while its sibling Sell P2P Matched Cost (`{time, value}`) still showed
~$0.50/kWh for 45 intervals. His review of PR #1592 then reproduced two
defects in the first version of this change, both pinned here:

* an OLDER non-zero rate beat a NEWER explicit no-match, because freshness
  was consulted only among non-zero values;
* sparse 5-minute rows took their duration from the spacing between rows,
  so an omitted (unmatched) interval was filled with the held rate.

The five states the observation path keeps apart: an unavailable source, an
uncovered interval, an explicit no-match, a valid zero-priced match, and a
valid positive or negative rate.

Sell P2P Matched Cost's contract is from purcell-lab/localvolts_v2
`haeo_feed.py`: key `sell_matched_cost`, unit `$/kWh`, `source_field:
matchedCost`, `interpolation_mode: previous`, value `matched_price()` =
matchedCost / (volume x proportionP2P), stamped at the interval START
(`intervalEnd` - 5 min), and an interval with no matched energy OMITTED.
"""

from __future__ import annotations

import random
import statistics
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_shared
import solver_writer
from solver_inputs import prices

# The reference household's evening row (17:00-17:05 AEST, 5 Oct 2026).
REAL = {"proportionP2P": 0.569915, "matchedCost": 0.268861, "volume": 0.9368}
RATE = 0.268861 / (0.9368 * 0.569915)

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)  # 18:00 AEST
FIVE = timedelta(minutes=5)
LV_FEED_ATTRS = {
    "unit_of_measurement": "$/kWh",
    "source_field": "matchedCost",
    "interpolation_mode": "previous",
}


_BLOCKS = {
    "solver_p2p_block_1_rate_kw": 11.5,
    "solver_p2p_block_1_start_hour": 17,
    "solver_p2p_block_1_end_hour": 24,
}


def _grid(n: int, start: datetime = T0, step: timedelta = FIVE) -> list[datetime]:
    return [start + step * i for i in range(n)]


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _triple_row(start: datetime, **over) -> dict:
    return {"intervalEnd": (start + FIVE).isoformat(), **REAL, **over}


def _no_match_row(start: datetime) -> dict:
    return _triple_row(start, proportionP2P=0.0, matchedCost=0.0)


def _rate_row(start: datetime, value) -> dict:
    return {"time": start.isoformat(), "value": value}


def _triple_state(rows, updated=T0, **attrs) -> dict:
    return {
        "state": "26.5",
        "attributes": {"forecast": rows, **attrs},
        "last_updated": updated.isoformat() if updated else None,
    }


def _feed_state(rows, updated=T0, **attrs) -> dict:
    return {
        "state": "0.5",
        "attributes": {"forecast": rows, **LV_FEED_ATTRS, **attrs},
        "last_updated": updated.isoformat() if updated else None,
    }


def _run(states: dict, grid, primary, window=None, fallback=None):
    def fake_get(entity_id):
        if entity_id not in states:
            raise KeyError(entity_id)
        return states[entity_id]

    with patch.object(solver_writer, "ha_get", side_effect=fake_get):
        return solver_writer.resample_real_p2p_rate(
            grid, primary, window, fallback_sensor_id=fallback
        )


def _pre_1537(raw, grid_times, p2p_window_kw):
    """resample_real_p2p_rate's algorithm as it stood before this change
    (origin/main at v0.94.441), minus the ha_get read -- the reference the
    single-source raw-triple path must reproduce exactly."""
    pts = []
    for p in raw:
        try:
            end_t = solver_writer.parse_iso(
                p["time"] if "time" in p else p["intervalEnd"]
            )
            start_t = end_t - timedelta(minutes=5)
            vol = float(p.get("volume") or 0.0)
            prop = float(p.get("proportionP2P") or 0.0)
            cost = float(p.get("matchedCost") or 0.0)
            matched_vol = vol * prop
            rate = (cost / matched_vol) if matched_vol > 0.01 else 0.0
            pts.append((start_t, rate))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    if not pts:
        return [0.0 for _ in grid_times]
    last_real_time = pts[-1][0]
    real_positive_rates = [r for _, r in pts if r > 0.0]
    fallback_rate = (
        statistics.median(real_positive_rates) if real_positive_rates else 0.0
    )
    use_blocks = p2p_window_kw is not None and len(p2p_window_kw) == len(grid_times)
    out = []
    for i, gt in enumerate(grid_times):
        in_window = p2p_window_kw[i] > 0 if use_blocks else True
        if not in_window:
            out.append(0.0)
        elif gt <= last_real_time:
            val = pts[0][1]
            for t, v in pts:
                if t <= gt:
                    val = v
                else:
                    break
            out.append(float(val))
        elif use_blocks:
            out.append(float(fallback_rate))
        else:
            out.append(0.0)
    return out


class ShapeDetection(unittest.TestCase):
    def test_raw_triple_rows_are_the_triple(self) -> None:
        self.assertFalse(solver_writer._p2p_rows_are_rate_shape([_triple_row(T0)]))

    def test_reference_household_time_keyed_triple_is_the_triple(self) -> None:
        row = {"time": T0.isoformat(), **REAL}
        self.assertFalse(solver_writer._p2p_rows_are_rate_shape([row]))

    def test_time_value_rows_are_the_rate_shape(self) -> None:
        self.assertTrue(solver_writer._p2p_rows_are_rate_shape([_rate_row(T0, 0.5)]))

    def test_any_triple_field_keeps_the_triple(self) -> None:
        row = {"time": T0.isoformat(), "value": 0.5, "volume": 1.0}
        self.assertFalse(solver_writer._p2p_rows_are_rate_shape([row]))

    def test_empty_is_not_the_rate_shape(self) -> None:
        self.assertFalse(solver_writer._p2p_rows_are_rate_shape([]))

    def test_detection_ignores_the_entity_name(self) -> None:
        rows = [_rate_row(t, 0.5) for t in _grid(3)]
        for name in ("sensor.localvolts_v2_current_sell_rate", "sensor.anything"):
            out = _run({name: _feed_state(rows)}, _grid(3), name)
            self.assertEqual(out, [0.5, 0.5, 0.5])


class SparseFiveMinuteMatches(unittest.TestCase):
    """Mark's reproduction: rows only at 18:15 and 18:30 cover
    [18:15,18:20) and [18:30,18:35), nothing else."""

    ROWS = (_rate_row(_at(15), 0.5), _rate_row(_at(30), 0.5))

    def test_each_row_covers_only_its_own_interval(self) -> None:
        grid = _grid(8)  # 18:00 .. 18:35, every 5 minutes
        out = _run({"sensor.mc": _feed_state(list(self.ROWS))}, grid, "sensor.mc")
        self.assertEqual(out, [0.0, 0.0, 0.0, 0.5, 0.0, 0.0, 0.5, 0.0])

    def test_his_fifteen_minute_grid(self) -> None:
        grid = _grid(4, step=timedelta(minutes=15))  # 18:00 .. 18:45
        out = _run({"sensor.mc": _feed_state(list(self.ROWS))}, grid, "sensor.mc")
        self.assertEqual(out, [0.0, 0.5, 0.5, 0.0])

    def test_inside_a_block_an_omitted_row_is_no_match_past_it_is_uncovered(
        self,
    ) -> None:
        """Within the feed's horizon an omitted interval is the provider's
        explicit no-match (0). Past its last row the interval is uncovered,
        so a configured block keeps the median matched rate."""
        grid = [_at(20), _at(40)]
        out = _run(
            {"sensor.mc": _feed_state(list(self.ROWS))}, grid, "sensor.mc", [11.5, 11.5]
        )
        self.assertEqual(out, [0.0, 0.5])

    def test_without_the_provider_contract_rows_have_no_duration(self) -> None:
        """Generic `{time, value}` rows do not establish their own extent:
        not the LocalVolts feed and no `end` -- nothing is covered."""
        state = _feed_state(list(self.ROWS))
        del state["attributes"]["source_field"]
        out = _run({"sensor.mc": state}, [_at(15), _at(30)], "sensor.mc")
        self.assertEqual(out, [0.0, 0.0])

    def test_an_explicit_end_gives_the_extent(self) -> None:
        rows = [{"time": _at(15).isoformat(), "end": _at(45).isoformat(), "value": 0.4}]
        state = {
            "state": "0.4",
            "attributes": {"forecast": rows, "unit_of_measurement": "$/kWh"},
            "last_updated": T0.isoformat(),
        }
        out = _run({"sensor.g": state}, [_at(15), _at(40), _at(45)], "sensor.g")
        self.assertEqual(out, [0.4, 0.4, 0.0])


class Units(unittest.TestCase):
    GRID = (T0,)

    def test_cents_per_kwh_is_converted(self) -> None:
        state = _feed_state([_rate_row(T0, 50.0)], unit_of_measurement="c/kWh")
        out = _run({"sensor.mc": state}, list(self.GRID), "sensor.mc")
        self.assertAlmostEqual(out[0], 0.50, places=12)

    def test_dollars_per_kwh_is_used_as_is(self) -> None:
        state = _feed_state([_rate_row(T0, 0.5)])
        self.assertEqual(
            _run({"sensor.mc": state}, list(self.GRID), "sensor.mc"), [0.5]
        )

    def test_a_missing_unit_refuses_the_source(self) -> None:
        state = _feed_state([_rate_row(T0, 0.5)])
        del state["attributes"]["unit_of_measurement"]
        self.assertEqual(
            _run({"sensor.mc": state}, list(self.GRID), "sensor.mc"), [0.0]
        )

    def test_a_non_price_unit_refuses_the_source(self) -> None:
        for unit in ("kW", "%", "$"):
            with self.subTest(unit=unit):
                state = _feed_state([_rate_row(T0, 0.5)], unit_of_measurement=unit)
                out = _run({"sensor.mc": state}, list(self.GRID), "sensor.mc")
                self.assertEqual(out, [0.0])

    def test_the_household_currency_code_is_the_major_unit(self) -> None:
        hass = SimpleNamespace(config=SimpleNamespace(currency="AUD"))
        state = _feed_state([_rate_row(T0, 0.5)], unit_of_measurement="AUD/kWh")
        with patch.object(solver_writer.NATIVE, "hass", hass):
            out = _run({"sensor.mc": state}, list(self.GRID), "sensor.mc")
        self.assertEqual(out, [0.5])

    def test_another_currency_code_is_refused(self) -> None:
        hass = SimpleNamespace(config=SimpleNamespace(currency="AUD"))
        state = _feed_state([_rate_row(T0, 0.5)], unit_of_measurement="EUR/kWh")
        with patch.object(solver_writer.NATIVE, "hass", hass):
            out = _run({"sensor.mc": state}, list(self.GRID), "sensor.mc")
        self.assertEqual(out, [0.0])

    def test_a_refused_fallback_never_overrides_the_primary(self) -> None:
        bad = _feed_state([_rate_row(T0, 99.0)], updated=T0 + FIVE)
        del bad["attributes"]["unit_of_measurement"]
        states = {"sensor.a": _triple_state([_triple_row(T0)]), "sensor.b": bad}
        out = _run(states, self.GRID, "sensor.a", fallback="sensor.b")
        self.assertAlmostEqual(out[0], RATE, places=12)


class NewerNoMatchBeatsStaleMatch(unittest.TestCase):
    """Mark's reproduction (a): a current primary of [no match] and a one-hour-
    older secondary of [0.5] must give 0, not 0.5."""

    GRID = (T0,)

    def _states(self, primary_rows, secondary_rows, age=timedelta(hours=1)):
        return {
            "sensor.current_sell_rate": _triple_state(primary_rows, T0),
            "sensor.sell_matched_cost": _feed_state(secondary_rows, T0 - age),
        }

    def test_newer_explicit_no_match_wins(self) -> None:
        states = self._states([_no_match_row(T0)], [_rate_row(T0, 0.5)])
        out = _run(
            states,
            self.GRID,
            "sensor.current_sell_rate",
            fallback="sensor.sell_matched_cost",
        )
        self.assertEqual(out, [0.0])

    def test_field_order_does_not_change_the_answer(self) -> None:
        states = self._states([_no_match_row(T0)], [_rate_row(T0, 0.5)])
        out = _run(
            states,
            self.GRID,
            "sensor.sell_matched_cost",
            fallback="sensor.current_sell_rate",
        )
        self.assertEqual(out, [0.0])

    def test_mark_6_oct_shape_prices_no_p2p(self) -> None:
        """The live case: the fresh triple says no match all evening and the
        40-minute-old sibling still carries matches. Two projections of one
        provider are not independent evidence: the newer answer stands."""
        grid = _grid(6)
        states = self._states(
            [_no_match_row(t) for t in grid],
            [_rate_row(t, 0.50) for t in grid[1:5]],
            age=timedelta(minutes=40),
        )
        out = _run(
            states,
            grid,
            "sensor.current_sell_rate",
            fallback="sensor.sell_matched_cost",
        )
        self.assertEqual(out, [0.0] * 6)

    def test_where_the_newer_source_is_silent_the_older_one_answers(self) -> None:
        """The fallback's job: an interval the primary does not cover."""
        grid = [T0, T0 + FIVE]
        states = self._states([_no_match_row(T0)], [_rate_row(t, 0.5) for t in grid])
        out = _run(
            states,
            grid,
            "sensor.current_sell_rate",
            fallback="sensor.sell_matched_cost",
        )
        self.assertEqual(out, [0.0, 0.5])

    def test_an_unavailable_primary_uses_the_fallback(self) -> None:
        states = self._states([], [_rate_row(T0, 0.5)])
        out = _run(
            states, self.GRID, "sensor.gone", fallback="sensor.sell_matched_cost"
        )
        self.assertEqual(out, [0.5])

    def test_an_unavailable_state_is_unavailable(self) -> None:
        states = self._states([_triple_row(T0)], [_rate_row(T0, 0.5)])
        states["sensor.current_sell_rate"]["state"] = "unavailable"
        out = _run(
            states,
            self.GRID,
            "sensor.current_sell_rate",
            fallback="sensor.sell_matched_cost",
        )
        self.assertEqual(out, [0.5])


class GenuineZeroPricedMatch(unittest.TestCase):
    def test_a_zero_priced_match_is_an_answer_not_missing(self) -> None:
        """matchedCost 0 on real matched volume is a valid $0 rate: the newer
        source's 0 stands against an older 0.5."""
        states = {
            "sensor.a": _triple_state([_triple_row(T0, matchedCost=0.0)], T0),
            "sensor.b": _feed_state([_rate_row(T0, 0.5)], T0 - FIVE),
        }
        self.assertEqual(_run(states, [T0], "sensor.a", fallback="sensor.b"), [0.0])

    def test_a_zero_priced_match_counts_in_the_block_median_a_no_match_does_not(
        self,
    ) -> None:
        past = _at(60)  # beyond the feed's coverage, inside a block
        zero_match = _feed_state([_rate_row(T0, 0.0), _rate_row(_at(5), 0.6)])
        out = _run({"sensor.mc": zero_match}, [past], "sensor.mc", [11.5])
        self.assertAlmostEqual(out[0], 0.3, places=12)
        no_match = _feed_state([_rate_row(T0, None), _rate_row(_at(5), 0.6)])
        out = _run({"sensor.mc": no_match}, [past], "sensor.mc", [11.5])
        self.assertAlmostEqual(out[0], 0.6, places=12)

    def test_a_negative_rate_is_kept(self) -> None:
        state = _feed_state([_rate_row(T0, -0.05)])
        self.assertEqual(_run({"sensor.mc": state}, [T0], "sensor.mc"), [-0.05])


class ExpiredCoverage(unittest.TestCase):
    OLD = T0 - solver_writer.P2P_RATE_SOURCE_MAX_AGE - timedelta(minutes=1)

    def test_an_expired_fallback_does_not_fill_an_uncovered_interval(self) -> None:
        grid = [T0, T0 + FIVE]
        states = {
            "sensor.a": _triple_state([_triple_row(T0)], T0),
            "sensor.b": _feed_state([_rate_row(t, 0.5) for t in grid], self.OLD),
        }
        out = _run(states, grid, "sensor.a", fallback="sensor.b")
        self.assertAlmostEqual(out[0], RATE, places=12)
        self.assertEqual(out[1], 0.0)

    def test_an_expired_fallback_does_not_feed_the_block_median(self) -> None:
        states = {
            "sensor.a": _triple_state([_triple_row(T0, matchedCost=0.0)], T0),
            "sensor.b": _feed_state([_rate_row(T0, 0.9)], self.OLD),
        }
        out = _run(states, [T0, _at(60)], "sensor.a", [11.5, 11.5], "sensor.b")
        self.assertEqual(out, [0.0, 0.0])

    def test_an_expired_single_feed_prices_nothing(self) -> None:
        state = _feed_state([_rate_row(T0, 0.5)], self.OLD)
        self.assertEqual(_run({"sensor.mc": state}, [T0], "sensor.mc"), [0.0])

    def test_just_inside_the_age_limit_is_still_used(self) -> None:
        fresh = T0 - solver_writer.P2P_RATE_SOURCE_MAX_AGE
        state = _feed_state([_rate_row(T0, 0.5)], fresh)
        self.assertEqual(_run({"sensor.mc": state}, [T0], "sensor.mc"), [0.5])


class SnapshotTime(unittest.TestCase):
    def test_the_provider_timestamp_wins_over_the_entity_timestamp(self) -> None:
        state = _triple_state(
            [], T0, lastUpdate=(T0 - timedelta(minutes=30)).isoformat()
        )
        self.assertEqual(
            solver_writer._p2p_snapshot_time(state), T0 - timedelta(minutes=30)
        )

    def test_entity_timestamp_when_no_provider_timestamp(self) -> None:
        self.assertEqual(solver_writer._p2p_snapshot_time(_feed_state([], T0)), T0)

    def test_a_provider_timestamp_decides_the_conflict(self) -> None:
        """The triple's entity changed at T0 but the provider stamped its data
        30 minutes earlier; the feed's data is from 10 minutes earlier."""
        states = {
            "sensor.a": _triple_state(
                [_no_match_row(T0)],
                T0,
                lastUpdate=(T0 - timedelta(minutes=30)).isoformat(),
            ),
            "sensor.b": _feed_state([_rate_row(T0, 0.5)], T0 - timedelta(minutes=10)),
        }
        self.assertEqual(_run(states, [T0], "sensor.a", fallback="sensor.b"), [0.5])

    def test_a_tie_keeps_the_primary(self) -> None:
        states = {
            "sensor.a": _triple_state([_triple_row(T0)], T0),
            "sensor.b": _feed_state([_rate_row(T0, 0.6)], T0),
        }
        out = _run(states, [T0], "sensor.a", fallback="sensor.b")
        self.assertAlmostEqual(out[0], RATE, places=12)

    def test_an_unknown_snapshot_counts_as_oldest(self) -> None:
        states = {
            "sensor.a": _triple_state([_triple_row(T0)], None),
            "sensor.b": _feed_state([_rate_row(T0, 0.6)], T0 - FIVE),
        }
        self.assertEqual(_run(states, [T0], "sensor.a", fallback="sensor.b"), [0.6])


class BlocksStillGate(unittest.TestCase):
    """#1560's rule holds on the observation path."""

    GRID = _grid(4)

    def test_rate_shape_with_blocks_is_gated_to_the_blocks(self) -> None:
        rows = [_rate_row(t, 0.5) for t in self.GRID]
        nan = float("nan")
        out = _run(
            {"sensor.mc": _feed_state(rows)},
            self.GRID,
            "sensor.mc",
            [11.5, nan, 0.0, 11.5],
        )
        self.assertEqual(out, [0.5, 0.0, 0.0, 0.5])

    def test_without_blocks_uncovered_is_zero(self) -> None:
        rows = [_rate_row(self.GRID[0], 0.5)]
        out = _run({"sensor.mc": _feed_state(rows)}, list(self.GRID), "sensor.mc", None)
        self.assertEqual(out, [0.5, 0.0, 0.0, 0.0])


class CommitmentsAndVolumeUnchanged(unittest.TestCase):
    """A fallback rate observation must not create a commitment or change
    trade volume: the blocks and the P2P volume are read, never changed."""

    BLOCKS = _BLOCKS

    def test_fixed_export_is_the_same_with_or_without_the_fallback(self) -> None:
        grid = _grid(6)
        cfg = dict(self.BLOCKS)
        with_fb = {**cfg, "solver_p2p_matched_rate_forecast_sensor_2": "sensor.b"}
        self.assertEqual(
            solver_writer.fetch_p2p_fixed_export_kw(cfg, grid),
            solver_writer.fetch_p2p_fixed_export_kw(with_fb, grid),
        )

    def test_volume_is_the_same_with_or_without_the_fallback(self) -> None:
        grid = _grid(3)
        n = len(grid)
        states = {
            "sensor.a": _triple_state([_no_match_row(t) for t in grid]),
            "sensor.b": _feed_state([_rate_row(t, 0.5) for t in grid]),
        }

        def build(cfg):
            with patch.multiple(
                solver_writer,
                ha_get=lambda e: states.get(e, {"attributes": {"forecast": []}}),
                fetch_aemo_forecast=lambda _e: None,
                fetch_price_history=lambda *_a, **_k: [],
                compute_5min_offset=lambda *_a, **_k: {},
                compute_price_percentile_band=lambda *_a, **_k: {},
                resample_price_with_extrapolation=lambda *_a, **_k: (
                    [0.1] * n,
                    [True] * n,
                ),
                check_aemo_p5min_disagreement=lambda *_a, **_k: None,
                p2p_match_fraction=lambda **_k: 0.65,
                p2p_recent_avg_volume_kwh=lambda **_k: 60.0,
            ):
                return prices.build_price_arrays(
                    {
                        "solver_import_price_sensor": "sensor.i",
                        "solver_export_price_sensor": "sensor.e",
                        "solver_p2p_matched_rate_forecast_sensor": "sensor.a",
                        **cfg,
                    },
                    grid,
                    n,
                    grid[0],
                    True,
                    "sensor.array",
                )

        a = build(dict(self.BLOCKS))
        b = build(
            {**self.BLOCKS, "solver_p2p_matched_rate_forecast_sensor_2": "sensor.b"}
        )
        self.assertEqual(a.p2p_recent_volume_kwh, b.p2p_recent_volume_kwh)


class SingleRawTripleIsUnchanged(unittest.TestCase):
    """With one source configured and raw-triple rows, the output equals the
    pre-#1537 algorithm exactly (==, not approximately)."""

    def _cases(self):
        rng = random.Random(1537)
        for _ in range(200):
            n_rows = rng.randint(0, 40)
            start = T0 + FIVE * rng.randint(-6, 6)
            rows = []
            for i in range(n_rows):
                t = start + FIVE * i
                row = {
                    "volume": round(rng.uniform(0, 2), 4),
                    "proportionP2P": rng.choice([0.0, round(rng.random(), 6)]),
                    "matchedCost": round(rng.uniform(-0.1, 0.9), 6),
                }
                key = rng.choice(["intervalEnd", "time"])
                row[key] = (t + FIVE).isoformat()
                if rng.random() < 0.05:
                    row = {"volume": 1.0}  # no time key: skipped by both
                rows.append(row)
            rng.shuffle(rows)
            grid = _grid(rng.randint(1, 60), T0 + FIVE * rng.randint(-3, 3))
            window = rng.choice(
                [
                    None,
                    [rng.choice([0.0, 11.5, float("nan")]) for _ in grid],
                    [5.0],  # wrong length: treated as no blocks
                ]
            )
            yield rows, grid, window

    def test_byte_identical_to_the_pre_1537_algorithm(self) -> None:
        for rows, grid, window in self._cases():
            want = _pre_1537(rows, grid, window)
            got = _run(
                {"sensor.p2p": {"attributes": {"forecast": rows}}},
                grid,
                "sensor.p2p",
                window,
            )
            self.assertEqual(got, want)

    def test_unchanged_even_when_the_entity_reads_unknown(self) -> None:
        rows = [_triple_row(t) for t in _grid(3)]
        state = {"state": "unknown", "attributes": {"forecast": rows}}
        got = _run({"sensor.p2p": state}, _grid(3), "sensor.p2p")
        self.assertEqual(got, _pre_1537(rows, _grid(3), None))

    def test_the_same_sensor_twice_is_one_source(self) -> None:
        grid = _grid(3)
        rows = [_triple_row(t) for t in grid]
        got = _run(
            {"sensor.p2p": _triple_state(rows)},
            grid,
            "sensor.p2p",
            fallback="sensor.p2p",
        )
        self.assertEqual(got, _pre_1537(rows, grid, None))

    def test_blank_is_still_flat_zero(self) -> None:
        self.assertEqual(_run({}, _grid(3), None), [0.0, 0.0, 0.0])
        self.assertEqual(_run({}, _grid(3), "sensor.gone"), [0.0, 0.0, 0.0])


class NativeReadCarriesLastUpdated(unittest.TestCase):
    def test_native_ha_get_returns_last_updated(self) -> None:
        when = datetime(2026, 10, 6, 5, 43, 59, tzinfo=UTC)
        state = SimpleNamespace(
            entity_id="sensor.x", state="1", attributes={}, last_updated=when
        )
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: state))
        with patch.object(solver_shared.NATIVE, "hass", hass):
            out = solver_shared.ha_get("sensor.x")
        self.assertEqual(out["last_updated"], when.isoformat())

    def test_a_state_without_last_updated_still_reads(self) -> None:
        state = SimpleNamespace(entity_id="sensor.x", state="1", attributes={})
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: state))
        with patch.object(solver_shared.NATIVE, "hass", hass):
            out = solver_shared.ha_get("sensor.x")
        self.assertNotIn("last_updated", out)


if __name__ == "__main__":
    unittest.main()
