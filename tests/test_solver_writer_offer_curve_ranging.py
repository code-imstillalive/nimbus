"""Direct test coverage for solver_writer.py's publish_offer_curve()
publishing the new ranging attributes (nimbus issue #676) -- the exact
real $/kWh price interval each offer-curve step's own kW value holds
for, alongside the pre-existing import_curve/export_curve pairs.

Uses a lightweight stand-in for Plan (SimpleNamespace with just the
attributes publish_offer_curve() reads) rather than a real solved Plan --
the conversion/serialization logic under test here is independent of how
the numbers were computed; solver/network.py's own tests
(test_solver_offer_curve.py) already cover that the ranging values
themselves are real and correctly converted.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


def _plan(
    *,
    offer_curve_import=None,
    offer_curve_export=None,
    offer_curve_import_ranging=None,
    offer_curve_export_ranging=None,
    offer_curve_sweep_seconds=None,
    grid_import_kw=None,
):
    return SimpleNamespace(
        offer_curve_import=offer_curve_import,
        offer_curve_export=offer_curve_export,
        offer_curve_import_ranging=offer_curve_import_ranging,
        offer_curve_export_ranging=offer_curve_export_ranging,
        offer_curve_sweep_seconds=offer_curve_sweep_seconds,
        grid_import_kw=grid_import_kw if grid_import_kw is not None else [6.171],
    )


class TestRoundOfferCurveRanging(unittest.TestCase):
    def test_none_input_stays_none(self):
        self.assertIsNone(solver_writer._round_offer_curve_ranging(None))

    def test_rounds_each_interval_to_four_decimal_places(self):
        result = solver_writer._round_offer_curve_ranging(
            [(0.123456, 0.654321), (-1.0, float("inf"))]
        )
        self.assertEqual(result, [[0.1235, 0.6543], [-1.0, float("inf")]])

    def test_preserves_none_entries_for_invalid_ranging_steps(self):
        result = solver_writer._round_offer_curve_ranging(
            [(0.1, 0.2), None, (0.3, 0.4)]
        )
        self.assertEqual(result, [[0.1, 0.2], None, [0.3, 0.4]])


class TestPublishOfferCurveRangingAttributes(unittest.TestCase):
    def test_no_op_when_curves_absent(self):
        plan = _plan()
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_offer_curve(plan)
        post.assert_not_called()

    def test_publishes_ranging_alongside_the_existing_curves(self):
        plan = _plan(
            offer_curve_import=[(-1.0, 30.0), (0.1905, 6.171)],
            offer_curve_export=[(-1.0, 0.0), (0.0868, 0.0)],
            offer_curve_import_ranging=[(-1.0, 0.0), (0.1, float("inf"))],
            offer_curve_export_ranging=[None, (0.05, 0.15)],
            offer_curve_sweep_seconds=0.0922,
        )
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_offer_curve(plan)
        post.assert_called_once()
        entity_id, state, attrs = post.call_args[0]
        self.assertEqual(entity_id, "sensor.nimbus_offer_curve")
        self.assertEqual(state, 6.171)
        # Pre-existing attributes are completely unchanged in shape --
        # zero risk to any existing consumer of import_curve/export_curve.
        self.assertEqual(attrs["import_curve"], [[-1.0, 30.0], [0.1905, 6.171]])
        self.assertEqual(attrs["export_curve"], [[-1.0, 0.0], [0.0868, 0.0]])
        # New attributes, same index/order as their sibling curve.
        self.assertEqual(
            attrs["import_curve_ranging"], [[-1.0, 0.0], [0.1, float("inf")]]
        )
        self.assertEqual(attrs["export_curve_ranging"], [None, [0.05, 0.15]])

    def test_ranging_attributes_are_none_when_ranging_lists_are_none(self):
        # A real, honest case: offer_curve_import/export populated but
        # their own parallel ranging lists weren't (defensive -- not
        # expected from a real build_plan() call, but publish_offer_
        # curve() must not crash if it ever happens).
        plan = _plan(
            offer_curve_import=[(0.1905, 6.171)],
            offer_curve_export=[(0.0868, 0.0)],
            offer_curve_import_ranging=None,
            offer_curve_export_ranging=None,
        )
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_offer_curve(plan)
        _entity_id, _state, attrs = post.call_args[0]
        self.assertIsNone(attrs["import_curve_ranging"])
        self.assertIsNone(attrs["export_curve_ranging"])


if __name__ == "__main__":
    unittest.main()
