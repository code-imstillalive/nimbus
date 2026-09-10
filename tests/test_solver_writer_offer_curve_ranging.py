"""Direct test coverage for solver_writer.py's publish_offer_curve()
publishing the ranging attributes (nimbus issue #676) and the dict-
shaped curve attributes (nimbus issue #677) -- the exact real $/kWh
price interval each offer-curve step's own kW value holds for, and the
`{price: kW}` shape Mark Purcell requested instead of a list of pairs.

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


class TestOfferCurvePriceKey(unittest.TestCase):
    def test_fixed_four_decimal_formatting(self):
        self.assertEqual(solver_writer._offer_curve_price_key(0.1), "0.1000")
        self.assertEqual(solver_writer._offer_curve_price_key(-1.0), "-1.0000")
        self.assertEqual(
            solver_writer._offer_curve_price_key(0.21179999999999999), "0.2118"
        )


class TestBuildOfferCurveDicts(unittest.TestCase):
    def test_none_ranging_stays_none(self):
        curve_dict, ranging_dict = solver_writer._build_offer_curve_dicts(
            [(0.1905, 6.171)], None, "import"
        )
        self.assertEqual(curve_dict, {"0.1905": 6.171})
        self.assertIsNone(ranging_dict)

    def test_ranging_keyed_by_the_same_price_string_as_the_curve(self):
        curve_dict, ranging_dict = solver_writer._build_offer_curve_dicts(
            [(-1.0, 30.0), (0.1905, 6.171)],
            [(-1.0, 0.0), (0.1, float("inf"))],
            "import",
        )
        self.assertEqual(curve_dict, {"-1.0000": 30.0, "0.1905": 6.171})
        self.assertEqual(
            ranging_dict, {"-1.0000": [-1.0, 0.0], "0.1905": [0.1, float("inf")]}
        )

    def test_none_ranging_entry_preserved_for_an_invalid_step(self):
        curve_dict, ranging_dict = solver_writer._build_offer_curve_dicts(
            [(0.0868, 0.0)], [None], "export"
        )
        self.assertEqual(curve_dict, {"0.0868": 0.0})
        self.assertEqual(ranging_dict, {"0.0868": None})

    def test_a_genuine_collision_keeps_the_first_lower_price_entry(self):
        """nimbus issue #677's own real, named risk: two distinct
        unrounded sweep prices that round to the SAME 4dp key must not
        silently overwrite each other -- the curve is already
        price-sorted ascending, so keeping the FIRST occurrence means
        keeping the lower of the two colliding prices, deterministically,
        with a loud warning rather than a silent data loss. 0.12341 and
        0.12344 both genuinely round() to 0.1234 -- confirmed directly,
        not assumed, before writing this assertion."""
        curve_dict, ranging_dict = solver_writer._build_offer_curve_dicts(
            [(0.12341, 1.0), (0.12344, 2.0)],
            [(0.1, 0.13), (0.13, 0.15)],
            "import",
        )
        self.assertEqual(curve_dict, {"0.1234": 1.0})
        self.assertEqual(ranging_dict, {"0.1234": [0.1, 0.13]})

    def test_collision_logs_a_warning_naming_both_real_values(self):
        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as log:
            solver_writer._build_offer_curve_dicts(
                [(0.12341, 1.0), (0.12344, 2.0)], None, "import"
            )
        self.assertTrue(
            any("offer curve" in message for message in log.output),
            log.output,
        )


class TestPublishOfferCurveRangingAttributes(unittest.TestCase):
    def test_no_op_when_curves_absent(self):
        plan = _plan()
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_offer_curve(plan)
        post.assert_not_called()

    def test_publishes_ranging_alongside_the_dict_shaped_curves(self):
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
        # nimbus issue #677: dict shape, keyed by formatted price string.
        self.assertEqual(attrs["import_curve"], {"-1.0000": 30.0, "0.1905": 6.171})
        self.assertEqual(attrs["export_curve"], {"-1.0000": 0.0, "0.0868": 0.0})
        # Ranging dicts, keyed by the SAME price strings as their sibling curve.
        self.assertEqual(
            attrs["import_curve_ranging"],
            {"-1.0000": [-1.0, 0.0], "0.1905": [0.1, float("inf")]},
        )
        self.assertEqual(
            attrs["export_curve_ranging"], {"-1.0000": None, "0.0868": [0.05, 0.15]}
        )

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
