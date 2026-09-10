"""Direct test coverage for solver_writer.py's publish_offer_curve() --
the offer curve's own JSON shape (nimbus issue #706, superseding #677's
`{price: kW}` dict) and the price_limits attribute (nimbus issue #705).

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


class TestBuildOfferCurveBands(unittest.TestCase):
    def test_one_self_contained_band_per_real_breakpoint(self):
        bands = solver_writer._build_offer_curve_bands(
            [(-1.0, 30.0), (0.1905, 6.171)],
            [(-1.0, 0.0), (0.1, float("inf"))],
        )
        self.assertEqual(
            bands,
            [
                {"price_lower": -1.0, "price_upper": 0.0, "kw": 30.0},
                {"price_lower": 0.1, "price_upper": float("inf"), "kw": 6.171},
            ],
        )

    def test_missing_ranging_list_gives_every_band_null_bounds(self):
        # A real, honest case: offer_curve_import/export populated but
        # their own parallel ranging list wasn't -- kw is still the
        # real solved value, only the price range is unknown.
        bands = solver_writer._build_offer_curve_bands([(0.1905, 6.171)], None)
        self.assertEqual(
            bands, [{"price_lower": None, "price_upper": None, "kw": 6.171}]
        )

    def test_a_single_invalid_step_gives_that_band_null_bounds_only(self):
        bands = solver_writer._build_offer_curve_bands(
            [(-1.0, 0.0), (0.0868, 0.0)], [(-1.0, 0.05), None]
        )
        self.assertEqual(
            bands,
            [
                {"price_lower": -1.0, "price_upper": 0.05, "kw": 0.0},
                {"price_lower": None, "price_upper": None, "kw": 0.0},
            ],
        )

    def test_two_distinct_prices_rounding_to_the_same_4dp_key_both_survive(self):
        # nimbus issue #706: the exact real scenario that used to collide
        # and silently drop one entry under #677's `{price: kW}` dict
        # shape -- both genuinely distinct bands are now kept, in full,
        # with no dedup logic needed at all.
        bands = solver_writer._build_offer_curve_bands(
            [(0.12341, 1.0), (0.12344, 2.0)],
            [(0.1, 0.123415), (0.123415, 0.15)],
        )
        self.assertEqual(len(bands), 2)
        self.assertEqual(bands[0]["kw"], 1.0)
        self.assertEqual(bands[1]["kw"], 2.0)

    def test_kw_and_price_bounds_are_rounded_for_display(self):
        bands = solver_writer._build_offer_curve_bands(
            [(0.21179999999999999, 6.17123456)],
            [(0.099999999, 0.30000001)],
        )
        self.assertEqual(bands[0]["price_lower"], 0.1)
        self.assertEqual(bands[0]["price_upper"], 0.3)
        self.assertEqual(bands[0]["kw"], 6.171)

    def test_adjacent_exact_duplicates_collapse_to_one_row(self):
        # nimbus issue #706: two raw solves that round to the identical
        # (price_lower, price_upper, kw) triple carry zero extra
        # information -- collapse them, unlike the #677 collision case
        # (two DIFFERENT values silently colliding), which is why this
        # is safe where that wasn't.
        bands = solver_writer._build_offer_curve_bands(
            [(-1.0, 30.0), (0.04287104246724765, 30.0), (0.04287204246724765, 29.446)],
            [(-1.0, 0.0429), (-1.0, 0.0429), (0.0429, 0.043)],
        )
        self.assertEqual(
            bands,
            [
                {"price_lower": -1.0, "price_upper": 0.0429, "kw": 30.0},
                {"price_lower": 0.0429, "price_upper": 0.043, "kw": 29.446},
            ],
        )

    def test_near_duplicates_that_differ_after_rounding_both_survive(self):
        # The real bug this whole shape change fixes: rows that are
        # CLOSE but not identical after rounding must never collapse --
        # only true, indistinguishable-to-a-consumer duplicates do.
        bands = solver_writer._build_offer_curve_bands(
            [(0.1821, 0.291), (0.1822, 28.372)],
            [(0.1721, 0.1821), (0.1821, 0.3486)],
        )
        self.assertEqual(len(bands), 2)
        self.assertEqual(bands[0]["kw"], 0.291)
        self.assertEqual(bands[1]["kw"], 28.372)

    def test_infinite_bounds_survive_rounding_unchanged(self):
        bands = solver_writer._build_offer_curve_bands(
            [(-1.0, 30.0)], [(float("-inf"), float("inf"))]
        )
        self.assertEqual(bands[0]["price_lower"], float("-inf"))
        self.assertEqual(bands[0]["price_upper"], float("inf"))


class TestPublishOfferCurveAttributes(unittest.TestCase):
    def test_no_op_when_curves_absent(self):
        plan = _plan()
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_offer_curve(plan)
        post.assert_not_called()

    def test_publishes_band_shaped_curves(self):
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
        # nimbus issue #706: list of self-contained bands, not a
        # {price: kW} dict with a separate parallel _ranging dict.
        self.assertEqual(
            attrs["import_curve"],
            [
                {"price_lower": -1.0, "price_upper": 0.0, "kw": 30.0},
                {"price_lower": 0.1, "price_upper": float("inf"), "kw": 6.171},
            ],
        )
        self.assertEqual(
            attrs["export_curve"],
            [
                {"price_lower": None, "price_upper": None, "kw": 0.0},
                {"price_lower": 0.05, "price_upper": 0.15, "kw": 0.0},
            ],
        )
        self.assertNotIn("import_curve_ranging", attrs)
        self.assertNotIn("export_curve_ranging", attrs)

    def test_price_limits_published_as_a_real_json_object(self):
        # nimbus issue #705: the real AEMO domain the walk was bounded by,
        # not just a Python constant no consumer can read.
        from solver.network import _OFFER_CURVE_DOMAIN_MAX, _OFFER_CURVE_DOMAIN_MIN

        plan = _plan(
            offer_curve_import=[(-1.0, 30.0), (0.1905, 6.171)],
            offer_curve_export=[(-1.0, 0.0), (0.0868, 0.0)],
            offer_curve_import_ranging=[(-1.0, 0.0), (0.1, float("inf"))],
            offer_curve_export_ranging=[None, (0.05, 0.15)],
        )
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_offer_curve(plan)
        _entity_id, _state, attrs = post.call_args[0]
        self.assertEqual(
            attrs["price_limits"],
            {
                "market_floor_price": _OFFER_CURVE_DOMAIN_MIN,
                "market_price_cap": _OFFER_CURVE_DOMAIN_MAX,
                "unit": "$/kWh",
                "source": "AEMO Market Floor Price / Market Price Cap "
                "(nimbus issue #705, confirmed by Mark Purcell)",
            },
        )
        # The real, confirmed-current cap (nimbus issue #705) -- not the
        # old, never-independently-verified $20.00 placeholder.
        self.assertAlmostEqual(_OFFER_CURVE_DOMAIN_MAX, 23.20)
        self.assertAlmostEqual(_OFFER_CURVE_DOMAIN_MIN, -1.00)

    def test_curve_bands_have_null_bounds_when_ranging_lists_are_none(self):
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
        self.assertEqual(
            attrs["import_curve"],
            [{"price_lower": None, "price_upper": None, "kw": 6.171}],
        )
        self.assertEqual(
            attrs["export_curve"],
            [{"price_lower": None, "price_upper": None, "kw": 0.0}],
        )


if __name__ == "__main__":
    unittest.main()
