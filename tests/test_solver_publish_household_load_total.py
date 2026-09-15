"""nimbus issue #735 stage 3: `publish_household_load_total_forecast()`,
hoisted out of `main()` into `solver_publish.py`.

The hoist was a prerequisite for stage 2, not a detour — the load-input
block that stage wants to extract had this `ha_post_state()` call sitting
inside it, so moving the block wholesale would have put a publish into an
inputs module and contradicted the very split #735 is building.

**Why this test exists at all.** Until the hoist, the only guard on this
publish was source-inspection: `test_solver_writer_load_total_state_
consistency.py` greps the file for `round(load_kw[0], 3)`. That was the
honest choice when the call lived 1,600 lines deep inside `main()` and
could not be reached without mocking an entire solve cycle. Now that it
is a function with an explicit signature, the same property can be
asserted **behaviourally** — the publish is actually performed and the
published values are read back.

That is strictly stronger. A grep proves the source contains a string; it
cannot notice that the string is inside a branch that never runs, or that
the value published is the right expression applied to the wrong array.

The property being guarded is nimbus issue **#100** (Mark Purcell, real
install): this sensor's `state` must be `load_kw[0]` **after** the live
whole-house cross-check anchor overwrites it, while `summed_18_now_kw` is
a snapshot taken *before* that overwrite. Publishing the snapshot as
`state` while `forecast[0].value` used the overwritten array made two
numbers disagree that any reasonable reader assumes are the same thing.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import numpy as np
import solver_publish
import solver_writer

N = 4
_NOW = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)


def _publish(**overrides):
    """Call the real function with a minimal but realistic argument set,
    capturing the ha_post_state() it performs."""
    kwargs = {
        "cfg": {"solver_inverter_self_consumption_kw": 0.4},
        "grid_times": [_NOW + timedelta(minutes=5 * i) for i in range(N)],
        "n_periods": N,
        "now": _NOW,
        "load_kw": np.array([2.5, 3.0, 3.5, 4.0]),
        "load_lower_kw": np.array([2.0, 2.5, 3.0, 3.5]),
        "load_upper_kw": np.array([3.0, 3.5, 4.0, 4.5]),
        "live_load_kw": 2.5,
        "whole_house_now_kw": 2.44,
        "load_forecast_entities": ["sensor.a", "sensor.b"],
        "load_forecast_error": None,
        "load_forecast_warnings": [],
        "load_forecast_source_used": "per_load_sum",
        "load_forecast_coverage_hours": 48.0,
        "failed_load_entities": [],
    }
    kwargs.update(overrides)
    with patch.object(solver_writer, "ha_post_state") as post:
        solver_publish.publish_household_load_total_forecast(**kwargs)
    assert post.call_count == 1, post.call_count
    entity_id, state, attrs = post.call_args[0]
    return entity_id, state, attrs


class TestTheHoistPreservedTheContract(unittest.TestCase):
    def test_it_publishes_the_expected_entity(self):
        entity_id, _state, _attrs = _publish()
        self.assertEqual(entity_id, "sensor.nimbus_household_load_total_forecast")

    def test_it_reaches_the_real_ha_post_state_through_the_deferred_import(self):
        """nimbus issue #861: relocating a function also relocates who its
        internal callers resolve. Patching `solver_writer.ha_post_state`
        must still intercept this call from the new module — if the
        deferred by-module import were replaced with a from-import, this
        would sail past the mock and attempt a live HTTP request."""
        _entity_id, _state, attrs = _publish()
        self.assertIn("forecast", attrs)


class TestIssue100(unittest.TestCase):
    """The behavioural version of the property the source-grep guards."""

    def test_state_is_the_post_anchor_load_kw_zero(self):
        _e, state, _a = _publish(load_kw=np.array([9.75, 1.0, 1.0, 1.0]))
        self.assertEqual(state, 9.75)

    def test_state_and_forecast_zero_agree(self):
        """The actual #100 bug: these two disagreed whenever a household
        configured the whole-house cross-check sensor. A grep for
        `round(load_kw[0], 3)` cannot check that they *match*."""
        _e, state, attrs = _publish(load_kw=np.array([7.125, 2.0, 2.0, 2.0]))
        self.assertEqual(state, attrs["forecast"][0]["value"])

    def test_they_still_agree_when_the_anchor_has_overwritten_index_zero(self):
        """The real shape of the bug: `load_kw[0]` overwritten in place by
        the live anchor while a pre-anchor snapshot survives elsewhere.
        Passing an already-overwritten array is exactly what `main()`
        does, and both published numbers must follow it."""
        overwritten = np.array([5.5, 3.0, 3.0, 3.0])
        _e, state, attrs = _publish(load_kw=overwritten, live_load_kw=5.5)
        self.assertEqual(state, 5.5)
        self.assertEqual(attrs["forecast"][0]["value"], 5.5)

    def test_summed_18_now_kw_is_not_a_parameter_at_all(self):
        """Structural guard. The pre-anchor snapshot is not in this
        function's signature, so it cannot be published here by accident —
        reintroducing it would be a visible API change rather than a
        one-word edit deep inside a 1,600-line function."""
        import inspect

        params = set(
            inspect.signature(
                solver_publish.publish_household_load_total_forecast
            ).parameters
        )
        self.assertNotIn("summed_18_now_kw", params)


class TestPublishedShape(unittest.TestCase):
    def test_the_forecast_has_one_entry_per_period(self):
        _e, _s, attrs = _publish()
        self.assertEqual(len(attrs["forecast"]), N)

    def test_each_entry_carries_the_band(self):
        _e, _s, attrs = _publish()
        first = attrs["forecast"][0]
        self.assertEqual(first["value"], 2.5)
        self.assertEqual(first["lower"], 2.0)
        self.assertEqual(first["upper"], 3.0)

    def test_the_source_entities_are_reported(self):
        _e, _s, attrs = _publish()
        self.assertEqual(attrs["source_entities"], ["sensor.a", "sensor.b"])

    def test_a_real_error_is_surfaced_not_swallowed(self):
        _e, _s, attrs = _publish(load_forecast_error="recorder unavailable")
        self.assertEqual(attrs["load_forecast_source_error"], "recorder unavailable")

    def test_failed_entities_are_surfaced(self):
        _e, _s, attrs = _publish(failed_load_entities=["sensor.dead"])
        self.assertEqual(attrs["failed_load_entities"], ["sensor.dead"])

    def test_the_live_cross_check_values_are_both_reported(self):
        """Two genuinely independent numbers, deliberately both published
        so a reader can compare them — see the #100 note about why the
        solver_config diagnostic keeps the pre-anchor snapshot."""
        _e, _s, attrs = _publish(whole_house_now_kw=2.44, live_load_kw=2.5)
        self.assertEqual(attrs["whole_house_cross_check_now_kw"], 2.44)
        self.assertEqual(attrs["whole_house_live_now_kw"], 2.5)


class TestTheLeadTimeScalarsAreActuallyWired(unittest.TestCase):
    """nimbus issue #937. The pure picker has its own unit tests; these
    assert the values genuinely **reach the published attributes**.

    That distinction is not pedantic here — it is the exact shape of
    #538/#692, where a field was added in one place and silently never
    reached the bridge that publishes it, so a household adjusting it had
    zero effect forever. A tested helper wired to nothing looks identical
    to a working feature from the outside.
    """

    def test_all_three_keys_are_always_present(self):
        """Stable attribute set: a key that appears and vanishes between
        cycles is #589's "empty attributes for one cycle" problem, and it
        would also put holes in the very lead-time series these exist to
        build."""
        _entity_id, _state, attrs = _publish()
        for key in (
            "load_forecast_plus_1h_kw",
            "load_forecast_plus_6h_kw",
            "load_forecast_plus_24h_kw",
        ):
            self.assertIn(key, attrs)

    def test_a_short_horizon_publishes_none_rather_than_clamping(self):
        """The default fixture grid spans 20 minutes, so every one of
        these lead times is beyond it. None is the honest answer;
        clamping to the last period would publish a 20-minute-ahead
        number under a key claiming 24 hours."""
        _entity_id, _state, attrs = _publish()
        self.assertIsNone(attrs["load_forecast_plus_1h_kw"])
        self.assertIsNone(attrs["load_forecast_plus_6h_kw"])
        self.assertIsNone(attrs["load_forecast_plus_24h_kw"])

    def test_a_real_horizon_publishes_the_right_period(self):
        """Hourly grid, value == index, so the published figure names the
        index it came from and a shifted pick is unmissable."""
        hours = 30
        _entity_id, _state, attrs = _publish(
            grid_times=[_NOW + timedelta(hours=i) for i in range(hours)],
            n_periods=hours,
            load_kw=np.arange(hours, dtype=float),
            load_lower_kw=np.arange(hours, dtype=float),
            load_upper_kw=np.arange(hours, dtype=float),
        )
        self.assertEqual(attrs["load_forecast_plus_1h_kw"], 1.0)
        self.assertEqual(attrs["load_forecast_plus_6h_kw"], 6.0)
        self.assertEqual(attrs["load_forecast_plus_24h_kw"], 24.0)

    def test_the_published_values_are_rounded_like_every_other_kw_figure(self):
        """3 dp, matching `forecast[i].value` itself — otherwise these
        would be the only kW figures on the sensor carrying full float
        noise."""
        hours = 30
        raw = np.full(hours, 1.23456789)
        _entity_id, _state, attrs = _publish(
            grid_times=[_NOW + timedelta(hours=i) for i in range(hours)],
            n_periods=hours,
            load_kw=raw,
            load_lower_kw=raw,
            load_upper_kw=raw,
        )
        self.assertEqual(attrs["load_forecast_plus_6h_kw"], 1.235)

    def test_they_are_plain_floats_not_numpy_scalars(self):
        """These go into a published attribute dict; `np.float64` is not
        what HA's own JSON encoder expects."""
        hours = 30
        _entity_id, _state, attrs = _publish(
            grid_times=[_NOW + timedelta(hours=i) for i in range(hours)],
            n_periods=hours,
            load_kw=np.arange(hours, dtype=float),
            load_lower_kw=np.arange(hours, dtype=float),
            load_upper_kw=np.arange(hours, dtype=float),
        )
        value = attrs["load_forecast_plus_24h_kw"]
        self.assertIsInstance(value, float)
        self.assertNotIsInstance(value, np.floating)


if __name__ == "__main__":
    unittest.main()
