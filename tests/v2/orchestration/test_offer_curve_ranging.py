"""The published offer curve and its price limits (#705, #706) (#1837)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_adjacent_exact_duplicates_collapse_to_one_row(self):
        # test_adjacent_exact_duplicates_collapse_to_one_row
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

        # test_730_real_import_curve_shape_collapses_to_two_bands
        # nimbus issue #730 (Mark Purcell, live finding): a walk step and
        # the always-separate retail solve landed in the same tied
        # degenerate basis -- two bands both open at -inf, one's range a
        # strict subset of the other's, same kw. Real live numbers,
        # verbatim from the bug report.
        bands = solver_writer._build_offer_curve_bands(
            [(0.02, -0.0), (0.05, -0.0), (0.08, 0.0)],
            [
                (float("-inf"), 0.0369),
                (float("-inf"), 0.0748),
                (0.0748, float("inf")),
            ],
        )
        self.assertEqual(
            bands,
            [
                {"price_lower": float("-inf"), "price_upper": 0.0748, "kw": -0.0},
                {"price_lower": 0.0748, "price_upper": float("inf"), "kw": 0.0},
            ],
        )

        # test_730_real_export_curve_shape_collapses_to_two_bands
        # Same incident, export side -- the nested pair is bands 2/3
        # this time (bands 1/2 are adjacent-but-disjoint, correctly kept
        # separate: no subset relation between them).
        bands = solver_writer._build_offer_curve_bands(
            [(0.03, 0.0), (0.09, -0.0), (0.25, 0.0)],
            [
                (float("-inf"), 0.0749),
                (0.075, float("inf")),
                (0.2162, float("inf")),
            ],
        )
        self.assertEqual(
            bands,
            [
                {"price_lower": float("-inf"), "price_upper": 0.0749, "kw": 0.0},
                {"price_lower": 0.075, "price_upper": float("inf"), "kw": -0.0},
            ],
        )


class TestPublishOfferCurveAttributes(unittest.TestCase):
    def test_publishes_band_shaped_curves(self):
        # test_publishes_band_shaped_curves
        # test_a_slow_curve_is_swept_every_three_minutes_not_every_cycle
        # #2132: a sweep over 30 s is refreshed only once the published curve
        # is 3 minutes old; a fast one is swept every cycle, as before.
        sweeps = []

        def complete(plan):
            sweeps.append(plan.seconds)
            return SimpleNamespace(
                offer_curve_deferred=None, offer_curve_sweep_seconds=plan.seconds
            )

        def pending(seconds):
            return SimpleNamespace(offer_curve_deferred=dict, seconds=seconds)

        run = solver_writer.sweep_offer_curve
        self.addCleanup(solver_writer._LAST_OFFER_CURVE.update, at=None, seconds=None)
        with patch.object(solver_writer.network, "complete_offer_curve", complete):
            solver_writer._LAST_OFFER_CURVE.update(at=None, seconds=None)
            run(pending(60.0), 0.0)
            self.assertIsNotNone(run(pending(60.0), 90.0).offer_curve_deferred)
            run(pending(60.0), 180.0)
            solver_writer._LAST_OFFER_CURVE.update(at=None, seconds=None)
            run(pending(1.0), 0.0)
            run(pending(1.0), 10.0)
            done = SimpleNamespace(offer_curve_deferred=None)
            self.assertIs(run(done, 20.0), done)
        self.assertEqual(sweeps, [60.0, 60.0, 1.0, 1.0])

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

        # test_price_limits_published_as_a_real_json_object
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
                # nimbus issue #1293: an explicit ISO code, not the ambiguous
                # symbol this used to carry. Deliberately NOT resolved against
                # hass.config.currency like every other money label: these two
                # numbers are AEMO's own Market Floor Price and Market Price
                # Cap, Australian market constants rather than the household's
                # money, so relabelling them EUR on a European install would be
                # a false statement about the value rather than a localisation.
                "unit": "AUD/kWh",
                "source": "AEMO Market Floor Price / Market Price Cap "
                "(nimbus issue #705, confirmed by Mark Purcell)",
            },
        )
        # The real, confirmed-current cap (nimbus issue #705) -- not the
        # old, never-independently-verified $20.00 placeholder.
        self.assertAlmostEqual(_OFFER_CURVE_DOMAIN_MAX, 23.20)
        self.assertAlmostEqual(_OFFER_CURVE_DOMAIN_MIN, -1.00)


if __name__ == "__main__":
    unittest.main()
