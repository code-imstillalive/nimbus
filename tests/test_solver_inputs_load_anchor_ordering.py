"""nimbus issue #735 stage 2: `build_load_arrays()` in
`solver_inputs/load.py`, and specifically the one property in it that a
future tidy-up is most likely to destroy.

`summed_18_now_kw` is snapshotted from `load_kw[0]` **before** the optional
live whole-house cross-check anchor overwrites that element in place. The
two are therefore *different numbers* on any install with the cross-check
configured — and that difference is nimbus issue **#100**, where
`sensor.nimbus_household_load_total_forecast`'s own `state` and
`forecast[0].value` silently disagreed.

Anything that "simplifies" the dataclass by recomputing `summed_18_now_kw`
from the returned `load_kw` reintroduces the bug. The block reads as
redundant — two variables that are equal most of the time — which is
exactly what makes it attractive to collapse, and exactly why it needs an
executable guard rather than only a comment.

The existing `#100` guards both sit at the *publish*
(`test_solver_publish_household_load_total.py`, and the source-inspection
test beside it). This one sits at the **source**: it asserts the two
values diverge where they are produced, so a regression is caught one
layer earlier and does not depend on the publish being wired correctly.

Deliberately narrow. `build_load_arrays()` does substantial real I/O and
the rest of its behaviour is already exercised end to end by the existing
suite; mocking all of it here would test the mocks. Only the ordering is
pinned.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from solver_inputs import load as load_inputs

N = 3
_NOW = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)
_GRID = [_NOW + timedelta(minutes=5 * i) for i in range(N)]

# The pre-anchor forecast: what the 18-circuit sum produced.
_FORECAST_FIRST = 4.0
# What the live whole-house meter says right now. Deliberately different,
# which is the whole point -- on a real install these rarely agree.
_LIVE_NOW = 1.25


def _build(cross_check="sensor.whole_house", live_state=str(_LIVE_NOW)):
    """Run the real build_load_arrays() with only the seams it needs."""
    cfg = {
        "solver_load_forecast_entities": ["sensor.a"],
        # Read with a bare cfg[...] rather than .get(), so it is
        # required even on the per-entity branch that never uses it.
        "solver_load_forecast_sensor": "",
        "solver_whole_house_cross_check_sensor": cross_check,
    }
    # Real arity, read off sw.sum_load_forecasts()'s own unpacking in
    # solver_inputs/load.py rather than guessed -- six values, and the
    # error is NOT among them.
    summed = (
        [_FORECAST_FIRST, 5.0, 6.0],  # load_kw
        [3.0, 4.0, 5.0],  # load_lower_kw
        [5.0, 6.0, 7.0],  # load_upper_kw
        [],  # failed_load_entities
        [],  # load_forecast_warnings
        48.0,  # load_forecast_coverage_hours
    )
    with (
        patch.object(solver_writer, "sum_load_forecasts", return_value=summed),
        patch.object(
            solver_writer, "resolve_load_forecast_source_label", return_value="summed"
        ),
        patch.object(solver_writer, "entity_exists", return_value=True),
        patch.object(
            solver_writer,
            "ha_get",
            return_value={"state": live_state, "attributes": {}},
        ),
        patch.object(solver_writer, "resample_forecast", return_value=[_LIVE_NOW]),
        patch.object(
            solver_writer, "_clear_load_forecast_error_notification_if_needed"
        ),
    ):
        return load_inputs.build_load_arrays(cfg, _GRID, N, _NOW)


class TestIssue100AtTheSource(unittest.TestCase):
    def test_the_snapshot_is_taken_before_the_anchor_overwrites(self):
        """The property itself: `summed_18_now_kw` keeps the pre-anchor
        forecast value while `load_kw[0]` carries the live one."""
        got = _build()
        self.assertEqual(got.summed_18_now_kw, _FORECAST_FIRST)
        self.assertEqual(got.load_kw[0], _LIVE_NOW)

    def test_the_two_values_genuinely_differ(self):
        """Stated separately and bluntly. If a future edit recomputes the
        snapshot from the returned array, these become equal and #100 is
        back — this assertion is the one that would fail."""
        got = _build()
        self.assertNotEqual(got.summed_18_now_kw, got.load_kw[0])

    def test_the_anchor_also_overwrites_the_band(self):
        """`lower[0]`/`upper[0]` are collapsed onto the anchored value too
        — a live reading has no uncertainty band."""
        got = _build()
        self.assertEqual(got.load_lower_kw[0], _LIVE_NOW)
        self.assertEqual(got.load_upper_kw[0], _LIVE_NOW)

    def test_later_periods_are_untouched_by_the_anchor(self):
        """The anchor is a *now* correction only. If it ever bled into
        later periods it would silently flatten the whole forecast."""
        got = _build()
        self.assertEqual(got.load_kw[1], 5.0)
        self.assertEqual(got.load_kw[2], 6.0)

    def test_without_a_cross_check_sensor_the_two_agree(self):
        """The common case, and the reason the pair looks redundant: with
        no cross-check configured there is no overwrite, so both read the
        forecast. That is precisely why the divergent case needs a test."""
        got = _build(cross_check="")
        self.assertEqual(got.summed_18_now_kw, _FORECAST_FIRST)
        self.assertEqual(got.load_kw[0], _FORECAST_FIRST)
        self.assertIsNone(got.live_load_kw)

    def test_an_unreadable_live_sensor_leaves_the_forecast_intact(self):
        """A non-numeric state must not corrupt period 0 — the real
        failure this try/except exists for."""
        got = _build(live_state="unavailable")
        self.assertEqual(got.load_kw[0], _FORECAST_FIRST)
        self.assertIsNone(got.live_load_kw)


class TestTheDataclassCarriesBoth(unittest.TestCase):
    def test_both_values_are_fields(self):
        """Structural: a caller must never have to reconstruct either one,
        which is how the wrong one gets chosen."""
        got = _build()
        self.assertTrue(hasattr(got, "summed_18_now_kw"))
        self.assertTrue(hasattr(got, "load_kw"))

    def test_the_result_is_frozen(self):
        """Frozen so the record of the construction is stable. It does not
        make the arrays immutable — they are genuinely mutated during
        construction, and pretending otherwise would be the lie."""
        got = _build()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            got.summed_18_now_kw = 99.0


if __name__ == "__main__":
    unittest.main()
