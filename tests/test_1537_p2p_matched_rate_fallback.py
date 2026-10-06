"""nimbus #1537 item 3: a `{time, value}` matched-rate feed, and a second
matched-rate source used where the first shows no match.

Found live by Mark Purcell, 6 Oct 2026: LocalVolts v2's Current Sell Rate
(raw triple, `intervalEnd` rows with volume / proportionP2P / matchedCost)
read proportionP2P 0 and matchedCost 0 on all 287 forecast rows, while its
sibling Sell P2P Matched Cost (`{time, value}`, $/kWh) -- derived from the
same coordinator data -- still showed ~$0.50/kWh for 45 intervals,
18:15-23:55. The two entities were updating out of step.

Sell P2P Matched Cost's semantics are from purcell-lab/localvolts_v2
`haeo_feed.py`: key `sell_matched_cost`, unit `$/kWh`, value
`matched_price()` = matchedCost / (volume x proportionP2P), stamped at the
interval START (`intervalEnd` - duration), and an interval with no matched
energy is OMITTED from the forecast (the value is None).
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

# The reference household's evening row (17:00-17:05 AEST, 5 Oct 2026).
REAL = {"proportionP2P": 0.569915, "matchedCost": 0.268861, "volume": 0.9368}
RATE = 0.268861 / (0.9368 * 0.569915)

T0 = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)  # 17:00 AEST
FIVE = timedelta(minutes=5)


def _grid(n: int, start: datetime = T0) -> list[datetime]:
    return [start + FIVE * i for i in range(n)]


def _triple_row(start: datetime, **over) -> dict:
    return {"intervalEnd": (start + FIVE).isoformat(), **REAL, **over}


def _rate_row(start: datetime, value: float) -> dict:
    return {"time": start.isoformat(), "value": value}


def _state(rows, updated=None, unit=None) -> dict:
    attrs: dict = {"forecast": rows}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    out: dict = {"attributes": attrs}
    if updated is not None:
        out["last_updated"] = updated.isoformat()
    return out


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
        """A row carrying `value` AND a triple field stays the triple, so a
        feed the old code read is read the same way."""
        row = {"time": T0.isoformat(), "value": 0.5, "volume": 1.0}
        self.assertFalse(solver_writer._p2p_rows_are_rate_shape([row]))

    def test_empty_is_not_the_rate_shape(self) -> None:
        self.assertFalse(solver_writer._p2p_rows_are_rate_shape([]))

    def test_detection_ignores_the_entity_name(self) -> None:
        rows = [_rate_row(t, 0.5) for t in _grid(3)]
        for name in ("sensor.localvolts_v2_current_sell_rate", "sensor.anything"):
            out = _run({name: _state(rows)}, _grid(3), name)
            self.assertEqual(out, [0.5, 0.5, 0.5])


class TimeValueInput(unittest.TestCase):
    def test_the_value_is_the_rate_at_the_interval_start(self) -> None:
        grid = _grid(3)
        rows = [_rate_row(grid[0], 0.48), _rate_row(grid[1], 0.52)]
        rows.append(_rate_row(grid[2], 0.50))
        out = _run({"sensor.mc": _state(rows)}, grid, "sensor.mc")
        self.assertEqual(out, [0.48, 0.52, 0.50])

    def test_an_omitted_interval_is_no_match_not_the_previous_rate(self) -> None:
        """LV v2 omits an unmatched interval; it must read 0, not hold the
        previous interval's rate across the gap."""
        grid = _grid(4)
        rows = [_rate_row(grid[0], 0.5), _rate_row(grid[2], 0.6)]
        rows.append(_rate_row(grid[3], 0.6))
        out = _run({"sensor.mc": _state(rows)}, grid, "sensor.mc")
        self.assertEqual(out, [0.5, 0.0, 0.6, 0.6])

    def test_before_the_first_matched_row_is_no_match(self) -> None:
        """Mark's 6 Oct feed matched only 18:15-23:55; 17:00-18:15 is
        omitted and must read 0, not the first matched rate."""
        grid = _grid(4)
        rows = [_rate_row(grid[2], 0.5), _rate_row(grid[3], 0.5)]
        out = _run({"sensor.mc": _state(rows)}, grid, "sensor.mc", [11.5] * 4)
        self.assertEqual(out, [0.0, 0.0, 0.5, 0.5])

    def test_a_none_value_is_skipped(self) -> None:
        grid = _grid(2)
        rows = [_rate_row(grid[0], 0.5), {"time": grid[1].isoformat(), "value": None}]
        out = _run({"sensor.mc": _state(rows)}, grid, "sensor.mc")
        self.assertEqual(out, [0.5, 0.0])

    def test_cents_per_kwh_is_converted(self) -> None:
        grid = _grid(1)
        out = _run(
            {"sensor.mc": _state([_rate_row(grid[0], 50.0)], unit="c/kWh")},
            grid,
            "sensor.mc",
        )
        self.assertAlmostEqual(out[0], 0.50, places=12)

    def test_dollars_per_kwh_is_used_as_is(self) -> None:
        grid = _grid(1)
        out = _run(
            {"sensor.mc": _state([_rate_row(grid[0], 0.5)], unit="$/kWh")},
            grid,
            "sensor.mc",
        )
        self.assertEqual(out, [0.5])


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
            got = _run({"sensor.p2p": _state(rows)}, grid, "sensor.p2p", window)
            self.assertEqual(got, want)

    def test_a_second_source_that_cannot_be_read_changes_nothing(self) -> None:
        for rows, grid, window in self._cases():
            want = _pre_1537(rows, grid, window)
            got = _run(
                {"sensor.p2p": _state(rows)},
                grid,
                "sensor.p2p",
                window,
                fallback="sensor.missing",
            )
            self.assertEqual(got, want)

    def test_the_same_sensor_twice_is_one_source(self) -> None:
        grid = _grid(3)
        rows = [_triple_row(t) for t in grid]
        got = _run(
            {"sensor.p2p": _state(rows)}, grid, "sensor.p2p", fallback="sensor.p2p"
        )
        self.assertEqual(got, _pre_1537(rows, grid, None))

    def test_blank_is_still_flat_zero(self) -> None:
        self.assertEqual(_run({}, _grid(3), None), [0.0, 0.0, 0.0])
        self.assertEqual(_run({}, _grid(3), "sensor.gone"), [0.0, 0.0, 0.0])


class FallbackChoosesTheNonTrivialSource(unittest.TestCase):
    """Mark's 6 Oct shape: the triple is all-zero, the {time, value} sibling
    still carries the evening's matches."""

    GRID = _grid(6)

    def _states(self, triple_updated, rate_updated):
        zero_rows = [
            _triple_row(t, proportionP2P=0.0, matchedCost=0.0) for t in self.GRID
        ]
        # Matched only in the middle four intervals (omitted elsewhere).
        rate_rows = [_rate_row(t, 0.50) for t in self.GRID[1:5]]
        return {
            "sensor.current_sell_rate": _state(zero_rows, triple_updated),
            "sensor.sell_matched_cost": _state(rate_rows, rate_updated),
        }

    def test_the_all_zero_triple_falls_back_to_the_matched_feed(self) -> None:
        # The triple is the FRESHER one, as live: still the non-zero source wins.
        states = self._states(T0, T0 - timedelta(minutes=40))
        out = _run(
            states,
            self.GRID,
            "sensor.current_sell_rate",
            fallback="sensor.sell_matched_cost",
        )
        self.assertEqual(out, [0.0, 0.5, 0.5, 0.5, 0.5, 0.0])

    def test_order_of_the_two_fields_does_not_matter(self) -> None:
        states = self._states(T0, T0 - timedelta(minutes=40))
        a = _run(
            states,
            self.GRID,
            "sensor.current_sell_rate",
            fallback="sensor.sell_matched_cost",
        )
        b = _run(
            states,
            self.GRID,
            "sensor.sell_matched_cost",
            fallback="sensor.current_sell_rate",
        )
        self.assertEqual(a, b)

    def test_only_the_fallback_configured_works_alone(self) -> None:
        states = self._states(T0, T0)
        out = _run(states, self.GRID, None, fallback="sensor.sell_matched_cost")
        self.assertEqual(out, [0.0, 0.5, 0.5, 0.5, 0.5, 0.0])

    def test_an_unreadable_primary_uses_the_fallback(self) -> None:
        states = self._states(T0, T0)
        out = _run(
            states, self.GRID, "sensor.gone", fallback="sensor.sell_matched_cost"
        )
        self.assertEqual(out, [0.0, 0.5, 0.5, 0.5, 0.5, 0.0])


class FreshnessTieBreak(unittest.TestCase):
    GRID = _grid(2)

    def _states(self, a_updated, b_updated):
        return {
            "sensor.a": _state([_triple_row(t) for t in self.GRID], a_updated),
            "sensor.b": _state([_rate_row(t, 0.60) for t in self.GRID], b_updated),
        }

    def test_the_fresher_source_wins_when_both_match(self) -> None:
        out = _run(
            self._states(T0, T0 - FIVE), self.GRID, "sensor.a", fallback="sensor.b"
        )
        for r in out:
            self.assertAlmostEqual(r, RATE, places=12)
        out = _run(
            self._states(T0 - FIVE, T0), self.GRID, "sensor.a", fallback="sensor.b"
        )
        self.assertEqual(out, [0.6, 0.6])

    def test_a_tie_keeps_the_primary(self) -> None:
        out = _run(self._states(T0, T0), self.GRID, "sensor.a", fallback="sensor.b")
        for r in out:
            self.assertAlmostEqual(r, RATE, places=12)

    def test_an_unknown_last_updated_counts_as_oldest(self) -> None:
        out = _run(self._states(None, T0), self.GRID, "sensor.a", fallback="sensor.b")
        self.assertEqual(out, [0.6, 0.6])


class BlocksStillGate(unittest.TestCase):
    """#1560's rule holds for the new shape and for the merge."""

    GRID = _grid(4)

    def test_rate_shape_with_blocks_is_gated_to_the_blocks(self) -> None:
        rows = [_rate_row(t, 0.5) for t in self.GRID]
        nan = float("nan")
        out = _run(
            {"sensor.mc": _state(rows)}, self.GRID, "sensor.mc", [11.5, nan, 0.0, 11.5]
        )
        self.assertEqual(out, [0.5, 0.0, 0.0, 0.5])

    def test_rate_shape_without_blocks_is_zero_past_the_forecast(self) -> None:
        rows = [_rate_row(self.GRID[0], 0.5)]
        out = _run({"sensor.mc": _state(rows)}, self.GRID, "sensor.mc", None)
        self.assertEqual(out, [0.5, 0.0, 0.0, 0.0])

    def test_rate_shape_with_blocks_keeps_the_median_past_the_forecast(self) -> None:
        rows = [_rate_row(self.GRID[0], 0.4), _rate_row(self.GRID[1], 0.6)]
        out = _run({"sensor.mc": _state(rows)}, self.GRID, "sensor.mc", [11.5] * 4)
        self.assertEqual(out[:2], [0.4, 0.6])
        # GRID[2] is the 0.0 end-of-run marker (still inside coverage);
        # GRID[3] is beyond coverage, so the block keeps the median.
        self.assertEqual(out[2], 0.0)
        self.assertAlmostEqual(out[3], 0.5, places=12)

    def test_the_merge_respects_the_blocks(self) -> None:
        states = {
            "sensor.a": _state(
                [_triple_row(t, proportionP2P=0.0) for t in self.GRID], T0
            ),
            "sensor.b": _state([_rate_row(t, 0.5) for t in self.GRID], T0),
        }
        out = _run(
            states, self.GRID, "sensor.a", [0.0, 11.5, 11.5, 0.0], fallback="sensor.b"
        )
        self.assertEqual(out, [0.0, 0.5, 0.5, 0.0])


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
