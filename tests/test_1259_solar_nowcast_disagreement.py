"""nimbus issue #1259: the PV forecast step between solar_kw[0] (the
live-measured anchor) and solar_kw[1] (the first genuine forecast
period).

The issue proposed blending the anchor forward over the first few periods
(a "nowcast decay"). That proposal was checked against the one real event
on record BEFORE being built, and rejected: every decay shape considered
would have made that event 2.8x-6.9x worse, because the forecast was
right and the anchor was the stale number. Mark Purcell's own decision,
verbatim: *"don't build mechanism 1... Leaving open as a measurement task
rather than an implementation one, unless a second real event shows a
different shape."*

`update_solar_nowcast_disagreement()` is that measurement task, and these
are its tests. Two of them are the reason the file exists at all:

- `test_the_real_1259_event_records_the_forecast_as_closer` replays the
  issue's own recorded numbers (anchor 1.78, forecast 15.89, actual
  17.88) end to end and asserts the measurement reaches the conclusion
  the arithmetic in the issue reached. That is the whole point of the
  instrument -- if it disagreed with the one event we already know the
  answer to, nothing it published later could be trusted.
- `test_a_reversed_event_records_the_anchor_as_closer` is the same
  replay with the actual on the other side, because an instrument that
  can only return one answer measures nothing.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ


def _cfg(**overrides):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_nowcast_measurement_enabled": True,
    }
    cfg.update(overrides)
    return cfg


def _grid(start, n=48, step_minutes=5):
    return [start + timedelta(minutes=i * step_minutes) for i in range(n)]


class _StateFileCase(unittest.TestCase):
    """Redirects SOLAR_DELIVERY_RATIO_PATH into a per-test temp file --
    the same requirement tests/test_main_golden_output_guardrail.py's own
    docstring records (this path's real default is
    /opt/nimbus_solver_solar_delivery_ratio.json, which is LIVE on either
    NUC), and the same setUp/tearDown shape
    tests/test_solar_delivery_ratio.py already uses."""

    def setUp(self):
        fd, self._tmpfile = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._patcher = patch.object(
            solver_writer, "SOLAR_DELIVERY_RATIO_PATH", self._tmpfile
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        try:
            os.remove(self._tmpfile)
        except OSError:
            pass

    def _state(self):
        with open(self._tmpfile, encoding="utf-8") as f:
            return json.load(f)


class TestGating(_StateFileCase):
    def test_switch_off_is_a_complete_no_op(self):
        """The switch defaults OFF, and off must mean off -- no history
        read, no state write, nothing published. This is the assertion
        that makes the change safe to merge onto a live install."""
        cfg = _cfg(solver_nowcast_measurement_enabled=False)
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.update_solar_nowcast_disagreement(
                cfg, now, _grid(now), [1.78] + [15.89] * 47
            )
            fetch.assert_not_called()
        self.assertIsNone(result)
        # Not even a state-file write: the file is still the empty
        # tempfile mkstemp created.
        self.assertEqual(os.path.getsize(self._tmpfile), 0)

    def test_missing_solar_power_sensor_is_a_no_op_even_when_switched_on(self):
        cfg = _cfg(solver_solar_power_sensor=None)
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.update_solar_nowcast_disagreement(
                cfg, now, _grid(now), [1.78] + [15.89] * 47
            )
            fetch.assert_not_called()
        self.assertIsNone(result)


class TestQueueing(_StateFileCase):
    def test_a_material_disagreement_is_queued_against_index_1s_own_time(self):
        cfg = _cfg()
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(now)
        # The real #1259 event's own two numbers.
        solar_kw = [1.78] + [15.89] * (len(grid_times) - 1)

        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.update_solar_nowcast_disagreement(
                cfg, now, grid_times, solar_kw
            )
            # Nothing to resolve on a first call -- no history read at all.
            fetch.assert_not_called()

        state = self._state()
        self.assertEqual(len(state["nowcast_pending"]), 1)
        queued = state["nowcast_pending"][0]
        # Queued against grid_times[1], NOT the ~60min horizon the #128
        # delivery ratio uses -- index 1 is the entire subject here.
        self.assertEqual(queued["target_time"], grid_times[1].isoformat())
        self.assertAlmostEqual(queued["anchor_kw"], 1.78)
        self.assertAlmostEqual(queued["forecast_kw"], 15.89)

        self.assertEqual(result["considered_count"], 1)
        self.assertEqual(result["disagreement_count"], 1)
        self.assertEqual(result["resolved_count"], 0)
        self.assertAlmostEqual(result["disagreement_now_kw"], 14.11, places=3)
        self.assertEqual(result["disagreement_rate"], 1.0)
        # Nothing graded yet, so the decisive fraction is honestly None
        # rather than a fabricated 0.0.
        self.assertIsNone(result["forecast_closer_fraction"])

    def test_agreement_is_counted_but_not_queued(self):
        cfg = _cfg()
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(now)
        # 0.5 kW apart: real daylight, but nowhere near the 3.0 kW bar.
        solar_kw = [15.0] + [15.5] * (len(grid_times) - 1)

        result = solver_writer.update_solar_nowcast_disagreement(
            cfg, now, grid_times, solar_kw
        )

        self.assertEqual(self._state()["nowcast_pending"], [])
        self.assertEqual(result["considered_count"], 1)
        self.assertEqual(result["disagreement_count"], 0)
        self.assertEqual(result["disagreement_rate"], 0.0)
        self.assertAlmostEqual(result["disagreement_now_kw"], 0.5, places=3)

    def test_dawn_dusk_levels_are_not_even_considered(self):
        """Reuses #128's own SOLAR_DELIVERY_MIN_FORECAST_KW floor: 0.4 vs
        3.6 kW at first light clears the 3.0 kW disagreement bar on raw
        arithmetic while meaning nothing physically. It must not enter
        the denominator either, or the published rate would be dominated
        by twilight."""
        cfg = _cfg()
        now = datetime(2026, 9, 27, 5, 45, tzinfo=BRISBANE)
        grid_times = _grid(now)
        solar_kw = [0.4] + [3.6] * (len(grid_times) - 1)

        result = solver_writer.update_solar_nowcast_disagreement(
            cfg, now, grid_times, solar_kw
        )

        self.assertEqual(self._state()["nowcast_pending"], [])
        self.assertEqual(result["considered_count"], 0)
        self.assertEqual(result["disagreement_count"], 0)
        self.assertIsNone(result["disagreement_rate"])
        self.assertIsNone(result["disagreement_now_kw"])


class TestResolution(_StateFileCase):
    def _queue_then_resolve(self, anchor_kw, forecast_kw, actual_kw):
        cfg = _cfg()
        t0 = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(t0)
        solar_kw = [anchor_kw] + [forecast_kw] * (len(grid_times) - 1)
        solver_writer.update_solar_nowcast_disagreement(cfg, t0, grid_times, solar_kw)

        # One period later, index 1's own timestamp has arrived. Feed the
        # measurement a real history row and let it grade both candidates.
        t1 = grid_times[1]

        def fake_fetch(entity_id, start, end):
            assert entity_id == "sensor.real_solar"
            return [(t1, actual_kw)]

        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=fake_fetch
        ):
            return solver_writer.update_solar_nowcast_disagreement(
                cfg,
                t1,
                _grid(t1),
                # Second cycle sits in agreement, so it contributes
                # nothing new to queue and cannot confound the counts.
                [forecast_kw] * len(grid_times),
            )

    def test_the_real_1259_event_records_the_forecast_as_closer(self):
        """The issue's own recorded numbers, replayed end to end:
        anchor 1.78 kW @12:30, forecast 15.89 kW @12:35, and the live
        sensor genuinely reading 17.875 kW at 12:35:11. Errors are
        1.99 kW (forecast) against 16.10 kW (anchor) -- so the PURE
        FORECAST was closer by 8.1x, which is exactly why mechanism 1
        was rejected rather than built."""
        result = self._queue_then_resolve(1.78, 15.89, 17.875)

        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["forecast_closer_count"], 1)
        self.assertEqual(result["anchor_closer_count"], 0)
        self.assertEqual(result["forecast_closer_fraction"], 1.0)
        self.assertAlmostEqual(result["mean_forecast_abs_error_kw"], 1.985, places=3)
        self.assertAlmostEqual(result["mean_anchor_abs_error_kw"], 16.095, places=3)
        # And the queue is drained -- an event is graded once, never
        # re-resolved into the same rolling window twice.
        self.assertEqual(self._state()["nowcast_pending"], [])

    def test_a_reversed_event_records_the_anchor_as_closer(self):
        """Same replay with reality landing on the anchor's side (a cloud
        edge that did NOT clear): the instrument must be able to return
        the other answer, or it is not measuring anything. This is the
        shape that would reopen #1259's mechanism 1."""
        result = self._queue_then_resolve(1.78, 15.89, 2.10)

        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["forecast_closer_count"], 0)
        self.assertEqual(result["anchor_closer_count"], 1)
        self.assertEqual(result["forecast_closer_fraction"], 0.0)

    def test_no_history_at_the_target_instant_is_dropped_not_scored_as_a_tie(self):
        cfg = _cfg()
        t0 = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(t0)
        solver_writer.update_solar_nowcast_disagreement(
            cfg, t0, grid_times, [1.78] + [15.89] * (len(grid_times) - 1)
        )
        t1 = grid_times[1]
        with patch.object(solver_writer, "fetch_entity_history_range", return_value=[]):
            result = solver_writer.update_solar_nowcast_disagreement(
                cfg, t1, _grid(t1), [15.89] * len(grid_times)
            )
        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(self._state()["nowcast_pending"], [])

    def test_events_older_than_the_rolling_window_are_trimmed(self):
        cfg = _cfg()
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        stale = now - timedelta(
            hours=solver_writer.SOLAR_NOWCAST_ROLLING_WINDOW_HOURS + 1
        )
        fresh = now - timedelta(minutes=30)
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pending": [],
                    "ratios": [],
                    "nowcast_pending": [],
                    "nowcast_events": [
                        {
                            "time": stale.isoformat(),
                            "anchor_abs_error_kw": 1.0,
                            "forecast_abs_error_kw": 9.0,
                        },
                        {
                            "time": fresh.isoformat(),
                            "anchor_abs_error_kw": 9.0,
                            "forecast_abs_error_kw": 1.0,
                        },
                    ],
                    "nowcast_considered": [stale.isoformat(), fresh.isoformat()],
                    "nowcast_disagreements": [stale.isoformat(), fresh.isoformat()],
                },
                f,
            )

        result = solver_writer.update_solar_nowcast_disagreement(
            cfg, now, _grid(now), [15.0] * 48
        )

        # The stale event, and the stale considered/disagreement stamps,
        # are all gone; only the fresh ones survive (plus this cycle's
        # own considered stamp, which is in agreement so adds no
        # disagreement).
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["forecast_closer_count"], 1)
        self.assertEqual(result["anchor_closer_count"], 0)
        self.assertEqual(result["considered_count"], 2)
        self.assertEqual(result["disagreement_count"], 1)


class TestSharedStateFile(_StateFileCase):
    """The two measurements share one JSON file. Neither may clobber the
    other's buffer -- a real hazard, because update_solar_delivery_ratio()
    used to write a freshly-built two-key dict over whatever was there."""

    def test_delivery_ratio_write_preserves_the_nowcast_buffer(self):
        cfg = _cfg()
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(now)
        solar_kw = [1.78] + [15.89] * (len(grid_times) - 1)

        solver_writer.update_solar_nowcast_disagreement(cfg, now, grid_times, solar_kw)
        self.assertEqual(len(self._state()["nowcast_pending"]), 1)

        with patch.object(solver_writer, "fetch_entity_history_range"):
            solver_writer.update_solar_delivery_ratio(cfg, now, grid_times, solar_kw)

        state = self._state()
        self.assertEqual(len(state["nowcast_pending"]), 1, "nowcast buffer was wiped")
        self.assertEqual(len(state["nowcast_considered"]), 1)
        # ...and the delivery ratio's own keys are still there too.
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(state["ratios"], [])

    def test_nowcast_write_preserves_the_delivery_ratio_buffer(self):
        cfg = _cfg()
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(now)
        solar_kw = [1.78] + [15.89] * (len(grid_times) - 1)

        with patch.object(solver_writer, "fetch_entity_history_range"):
            solver_writer.update_solar_delivery_ratio(cfg, now, grid_times, solar_kw)
        self.assertEqual(len(self._state()["pending"]), 1)

        solver_writer.update_solar_nowcast_disagreement(cfg, now, grid_times, solar_kw)

        state = self._state()
        self.assertEqual(len(state["pending"]), 1, "delivery ratio buffer was wiped")
        self.assertEqual(len(state["nowcast_pending"]), 1)

    def test_a_pre_1259_state_file_loads_without_a_nowcast_buffer(self):
        """A state file written by any earlier release has only
        pending/ratios. It must load cleanly, not be discarded -- a
        discarded file silently resets the #128 rolling ratio."""
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        fresh = now - timedelta(minutes=10)
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump(
                {"pending": [], "ratios": [{"time": fresh.isoformat(), "ratio": 0.9}]},
                f,
            )

        state = solver_writer._load_solar_delivery_state()
        self.assertEqual(len(state["ratios"]), 1)
        self.assertEqual(state["nowcast_pending"], [])
        self.assertEqual(state["nowcast_events"], [])


class TestPublishing(unittest.TestCase):
    """The published attribute is present only when the measurement
    actually ran. Absent-when-off is deliberate: an always-present null
    would widen this entity's attribute surface, and every golden-master
    snapshot with it, for a measurement no install has asked for yet."""

    def test_attribute_block_is_absent_when_the_measurement_did_not_run(self):
        src = solver_writer.__file__
        with open(src, encoding="utf-8") as f:
            text = f.read()
        # The conditional-unpack guard, not an unconditional key.
        self.assertIn('"solar_nowcast_check": (solar_delivery or {})', text)
        self.assertNotIn('"solar_nowcast_check": nowcast,', text)


class TestNoDispatchPath(unittest.TestCase):
    """#1259's own decision was that NOTHING adjusts the forecast. This
    is the guard for that: `solar_kw` must never be written to by the
    measurement, and no decay/blend weight may appear in the solar input
    path."""

    def test_the_measurement_never_mutates_solar_kw(self):
        cfg = _cfg()
        now = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
        grid_times = _grid(now)
        solar_kw = [1.78] + [15.89] * (len(grid_times) - 1)
        before = list(solar_kw)
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with patch.object(solver_writer, "SOLAR_DELIVERY_RATIO_PATH", tmp):
                solver_writer.update_solar_nowcast_disagreement(
                    cfg, now, grid_times, solar_kw
                )
        finally:
            os.remove(tmp)
        self.assertEqual(solar_kw, before)

    def test_solar_input_path_has_no_nowcast_blend(self):
        here = os.path.dirname(os.path.abspath(__file__))
        solar_py = os.path.join(
            os.path.dirname(here),
            "custom_components",
            "nimbus_load",
            "solver_inputs",
            "solar.py",
        )
        with open(solar_py, encoding="utf-8") as f:
            text = f.read()
        # build_solar_arrays() still anchors index 0 and ONLY index 0.
        self.assertIn("solar_kw[0] = max(0.0, live_solar_kw)", text)
        for banned in ("solar_kw[1] =", "solar_kw[2] =", "nowcast_weight", "decay_w"):
            self.assertNotIn(
                banned,
                text,
                f"{banned!r} appeared in the solar input path -- #1259's "
                "mechanism 1 was deliberately NOT built (it would have made "
                "the one measured event 2.8x-6.9x worse). If this is being "
                "revisited, reopen #1259 with the new measurement first.",
            )


if __name__ == "__main__":
    unittest.main()
