"""Real behavioural tests for solver_inputs/solar.py's
build_solar_arrays() -- nimbus issue #735 stage 1.

Every pre-existing guard on this logic (tests/test_solar_source_shape_
and_dedup.py's own wiring class, tests/test_solver_writer_solar_
fallback_not_crash.py in full) is source-INSPECTION style, for one
honest reason stated in both of their docstrings: the code lived inside
main(), a 1700-line function doing live ha_get/ha_post_state calls
throughout, and was not reachable end-to-end without mocking the entire
solve cycle.

Extracting it changed that. build_solar_arrays(cfg, grid_times,
n_periods) is now a directly callable function whose only outside
contact is a handful of solver_writer helpers, so the same properties
those tests assert by reading source text can finally be asserted by
RUNNING the real code. That is the concrete payoff of #735 stage 1
beyond line count, and these tests are deliberately written against
behaviour (returned arrays) rather than source text so they keep
holding if the implementation is rewritten.

Deliberately uses the REAL resample_forecast() and
_solar_entries_from_attributes() (pure functions, no HA contact) rather
than mocking them -- only the genuine I/O boundary (ha_get,
entity_exists, _kw_scale_factor) is patched. A test that mocked the
reshaping too would pass against a build_solar_arrays() that had
silently stopped reshaping anything.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from solver_inputs import solar as solar_inputs

_T0 = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
_N = 4
_GRID = [_T0 + timedelta(hours=i) for i in range(_N)]


def _forecast_attrs(values, lower=None, upper=None):
    """A generic forecast=[...] entity's attributes dict, the shape
    _solar_entries_from_attributes() reads first."""
    out = []
    for i, v in enumerate(values):
        entry = {"time": (_T0 + timedelta(hours=i)).isoformat(), "value": v}
        if lower is not None:
            entry["lower"] = lower[i]
        if upper is not None:
            entry["upper"] = upper[i]
        out.append(entry)
    return {"forecast": out}


class _SolarTestBase(unittest.TestCase):
    def setUp(self):
        # The log-once dedup caches are module-level; a leftover entry
        # from another test would silently suppress a warning this one
        # depends on.
        solver_writer._SOLAR_SOURCE_WARNED.clear()


class TestSingleSource(_SolarTestBase):
    def test_single_source_passes_through_with_its_own_bounds(self):
        states = {"sensor.a": _forecast_attrs([1.0, 2.0, 3.0, 4.0])}
        cfg = {"solver_solar_forecast_sensor": "sensor.a"}
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, lo, up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        self.assertEqual(kw, [1.0, 2.0, 3.0, 4.0])
        # No lower/upper published by the source -> a zero-width band,
        # never an invented one (elements.py's own
        # _validate_confidence_band requires lower <= value <= upper).
        self.assertEqual(lo, kw)
        self.assertEqual(up, kw)

    def test_negative_forecast_values_are_clamped_to_zero(self):
        """A ML forecaster can produce a tiny negative excursion near
        zero, physically impossible for solar -- found live on this
        script's very first real run."""
        states = {"sensor.a": _forecast_attrs([-0.5, 0.0, 2.0, -3.0])}
        cfg = {"solver_solar_forecast_sensor": "sensor.a"}
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, lo, _up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        self.assertEqual(kw, [0.0, 0.0, 2.0, 0.0])
        self.assertTrue(all(v >= 0.0 for v in lo))

    def test_source_bounds_are_clamped_to_straddle_the_value(self):
        states = {
            "sensor.a": _forecast_attrs(
                [2.0, 2.0, 2.0, 2.0],
                lower=[3.0, 1.0, 1.0, 1.0],  # [0] is above value: must clamp down
                upper=[1.0, 3.0, 3.0, 3.0],  # [0] is below value: must clamp up
            )
        }
        cfg = {"solver_solar_forecast_sensor": "sensor.a"}
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, lo, up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        for i in range(_N):
            self.assertLessEqual(lo[i], kw[i], f"lower > value at {i}")
            self.assertGreaterEqual(up[i], kw[i], f"upper < value at {i}")


class TestNoSourceFallback(_SolarTestBase):
    """The #115 regression guard, as real behaviour rather than a grep
    over source text: every configured source failing is what happens
    every single night on every solar install, and must not refuse to
    solve."""

    def _run_with_no_sources(self):
        cfg = {}
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": {}}),
            patch.object(solver_writer, "entity_exists", lambda e: False),
        ):
            return solar_inputs.build_solar_arrays(cfg, _GRID, _N)

    def test_no_sources_does_not_raise(self):
        kw, lo, up = self._run_with_no_sources()
        self.assertEqual(kw, [0.0] * _N)
        self.assertEqual(lo, [0.0] * _N)
        self.assertEqual(up, [0.0] * _N)

    def test_no_sources_returns_real_arrays_of_the_right_length(self):
        """Not None, not [], not a scalar -- the caller indexes these
        straight into elements.SolarConfig."""
        for arr in self._run_with_no_sources():
            self.assertIsInstance(arr, list)
            self.assertEqual(len(arr), _N)
            self.assertTrue(all(isinstance(v, float) for v in arr))

    def test_no_sources_still_logs_a_loud_warning(self):
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as caught:
            self._run_with_no_sources()
        self.assertTrue(
            any("no solar forecast source" in m for m in caught.output),
            f"expected the no-solar-data warning, got {caught.output}",
        )


class TestFailedSourceIsDroppedNotZeroFilled(_SolarTestBase):
    """The single most important property in this module: a failed solar
    source must be DROPPED from the blend, never averaged in as a
    phantom 0 kW, which would drag an otherwise-healthy blend down."""

    def test_unrecognized_shape_is_dropped_leaving_the_good_source_intact(self):
        states = {
            "sensor.good": _forecast_attrs([4.0, 4.0, 4.0, 4.0]),
            "sensor.bad": {"something_else": 1},  # no recognized shape
        }
        cfg = {
            "solver_solar_forecast_sensor": "sensor.good",
            "solver_solar_forecast_sensor_2": "sensor.bad",
        }
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, _lo, _up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        # 4.0, not 2.0 -- the bad source contributed nothing at all,
        # rather than being blended in as a zero.
        self.assertEqual(kw, [4.0] * _N)


class TestMultiSourceBlend(_SolarTestBase):
    def test_two_agreeing_sources_average_to_the_same_value(self):
        states = {
            "sensor.a": _forecast_attrs([2.0] * _N),
            "sensor.b": _forecast_attrs([2.0] * _N),
        }
        cfg = {
            "solver_solar_forecast_sensor": "sensor.a",
            "solver_solar_forecast_sensor_2": "sensor.b",
        }
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, lo, up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        for v in kw:
            self.assertAlmostEqual(v, 2.0)
        # Sources that agree add no extra uncertainty.
        for i in range(_N):
            self.assertAlmostEqual(lo[i], kw[i])
            self.assertAlmostEqual(up[i], kw[i])

    def test_disagreeing_sources_widen_the_confidence_band(self):
        """cross_source_spread() feeds real source DISAGREEMENT into the
        band, so the already-proven risk_aversion mechanism gets an
        earned signal instead of one source's own (possibly
        overconfident) bounds."""
        states = {
            "sensor.a": _forecast_attrs([1.0] * _N),
            "sensor.b": _forecast_attrs([5.0] * _N),
        }
        cfg = {
            "solver_solar_forecast_sensor": "sensor.a",
            "solver_solar_forecast_sensor_2": "sensor.b",
        }
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, lo, up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        for i in range(_N):
            self.assertAlmostEqual(kw[i], 3.0)
            self.assertLess(lo[i], kw[i], "band did not widen below")
            self.assertGreater(up[i], kw[i], "band did not widen above")


class TestLivePowerAnchor(_SolarTestBase):
    """The live anchor is deliberately scoped to index 0 ONLY -- every
    other period stays a genuine forecast, nothing propagates beyond
    'right now'."""

    def _run(self, live_state, scale=0.001, sensor="sensor.pv_power"):
        states = {
            "sensor.a": _forecast_attrs([2.0] * _N),
            sensor: None,
        }
        cfg = {
            "solver_solar_forecast_sensor": "sensor.a",
            "solver_solar_power_sensor": sensor,
        }

        def _ha_get(e):
            if e == sensor:
                return {"state": live_state, "attributes": {}}
            return {"attributes": states[e]}

        with (
            patch.object(solver_writer, "ha_get", _ha_get),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
            patch.object(solver_writer, "_kw_scale_factor", lambda e: scale),
        ):
            return solar_inputs.build_solar_arrays(cfg, _GRID, _N)

    def test_live_reading_overrides_period_zero_only(self):
        kw, lo, up = self._run("3800")  # 3800 W -> 3.8 kW
        self.assertAlmostEqual(kw[0], 3.8)
        # Zero-width band at the anchor: a measured value has no
        # forecast uncertainty to represent.
        self.assertAlmostEqual(lo[0], 3.8)
        self.assertAlmostEqual(up[0], 3.8)
        # Untouched beyond "now".
        for i in range(1, _N):
            self.assertAlmostEqual(kw[i], 2.0)

    def test_kw_native_sensor_is_not_divided_by_a_thousand(self):
        """nimbus issue #453 (Mark Purcell): this used to divide by
        1000.0 unconditionally, silently corrupting solar_kw[0] toward
        ~0 for any install whose sensor already reports kW."""
        kw, _lo, _up = self._run("3.8", scale=1.0)
        self.assertAlmostEqual(kw[0], 3.8)

    def test_unparseable_live_reading_leaves_the_forecast_value(self):
        kw, lo, up = self._run("unavailable")
        self.assertAlmostEqual(kw[0], 2.0)
        self.assertAlmostEqual(lo[0], 2.0)
        self.assertAlmostEqual(up[0], 2.0)

    def test_negative_live_reading_is_clamped_to_zero(self):
        kw, _lo, _up = self._run("-500")
        self.assertAlmostEqual(kw[0], 0.0)


class TestAutoIncludeDedup(_SolarTestBase):
    """nimbus issue #546: a configured source that resolves to a known
    Open-Meteo/Solcast entity is skipped as a STANDALONE member whenever
    the matching auto-include fetch is going to run anyway, so that
    integration is represented exactly once -- by its own full-coverage
    multi-entity read, never by a second, narrower read of one of its
    own entities."""

    def test_configured_known_entity_is_not_also_counted_standalone(self):
        known = "sensor.solcast_pv_forecast_forecast_today"
        self.assertTrue(
            solver_writer._is_known_solar_integration_entity(known),
            "fixture precondition: this must be a recognised known entity",
        )
        states = {known: _forecast_attrs([6.0] * _N)}
        cfg = {
            "solver_solar_forecast_sensor": known,
            "solver_auto_include_known_solar": True,
        }
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, _lo, _up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        # Represented exactly once: the auto-include Solcast read is the
        # only contributor, so the value is that source's own 6.0 rather
        # than a blend of it with a duplicate standalone read of itself.
        self.assertEqual(kw, [6.0] * _N)

    def test_with_auto_include_off_the_same_entity_is_read_standalone(self):
        """The mirror case -- the dedup must be conditional on
        auto-include actually being on, not unconditional."""
        known = "sensor.solcast_pv_forecast_forecast_today"
        states = {known: _forecast_attrs([6.0] * _N)}
        cfg = {
            "solver_solar_forecast_sensor": known,
            "solver_auto_include_known_solar": False,
        }
        with (
            patch.object(solver_writer, "ha_get", lambda e: {"attributes": states[e]}),
            patch.object(solver_writer, "entity_exists", lambda e: e in states),
        ):
            kw, _lo, _up = solar_inputs.build_solar_arrays(cfg, _GRID, _N)
        self.assertEqual(kw, [6.0] * _N)


if __name__ == "__main__":
    unittest.main()
