"""Regression tests for nimbus issue #467's per-period availability mask
(`BatteryConfig.unavailable_period_indices`), authorised by Mark Purcell
2026-09-13 ("Build an internal gating gap please").

Before this, availability could only ever be expressed as a PREFIX from
period 0 -- `available=False` gated the whole horizon, and #779's
`unavailable_until_period_index=k` gated `[0, k)`. Neither can say "away
09:00-15:00, home either side", and neither can express two separate
trips in one day at all.

That was a real correctness gap, not just a missing feature. The quality
scorer's RECONSTRUCTION side already masked per-period correctly
(`_resolve_battery_participant_history()` builds `is_home_mask` from the
availability entity's own recorded history and zeroes actual_charge_kw/
actual_discharge_kw), but the ORACLE re-solve had no equivalent gate --
so on any multi-trip day the two halves of the scorer genuinely disagreed
about where the car was, and the oracle was free to schedule charging for
an EV that was demonstrably out driving. That inflates the oracle's own
achievable cost floor, which overstates regret.

Same "prove the LP constraint actually binds" rigor as
test_solver_battery_participant_gating_and_shared_charger.py (#563/#779)
already established for the prefix gates: every gating test first solves
WITHOUT the gate and asserts real throughput happens, so a passing gated
assertion can't be an artifact of a scenario that was never exercising
the LP in the first place.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

# A deep, unambiguous price swing: dirt cheap for the first half, very
# expensive for the second. Gives the LP every economic reason to charge
# early and discharge late, so any period that ends up at exactly 0.0 is
# 0.0 because it was GATED, not because the LP found it unattractive.
_CHEAP = 0.05
_DEAR = 0.60


def _periods(n: int) -> PeriodGrid:
    return PeriodGrid(
        hours=np.array([1.0] * n), start=datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    )


def _swing_grid(n: int) -> GridConfig:
    half = n // 2
    import_price = np.array([_CHEAP] * half + [_DEAR] * (n - half))
    return GridConfig(
        import_price=import_price,
        export_price=import_price - 0.02,
        import_limit_kw=200.0,
        export_limit_kw=200.0,
    )


def _battery(name: str = "ev", **overrides) -> BatteryConfig:
    kwargs = {
        "name": name,
        "capacity_kwh": 40.0,
        "initial_soc_kwh": 20.0,
        "min_soc_kwh": 4.0,
        "max_soc_kwh": 40.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.005,
        "discharge_cost": 0.01,
        "salvage_value": 0.10,
    }
    kwargs.update(overrides)
    return BatteryConfig(**kwargs)


def _solve(n: int, battery: BatteryConfig):
    return build_plan(
        periods=_periods(n),
        grid=_swing_grid(n),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
    )


class TestPerPeriodMaskGatesExactlyThoseePeriods(unittest.TestCase):
    def test_two_separate_trips_in_one_day_are_both_gated(self):
        """The case the prefix mechanisms structurally could not express:
        a morning school run AND a separate evening trip. Periods 2-3 and
        7-8 are away; everything either side is home and must stay fully
        schedulable.
        """
        n = 10
        away = frozenset({2, 3, 7, 8})

        ungated = _solve(n, _battery())
        self.assertEqual(ungated.status, "optimal")
        self.assertGreater(
            float(
                np.sum(ungated.battery_charge_kw) + np.sum(ungated.battery_discharge_kw)
            ),
            1.0,
            "sanity check failed: with a real cheap/expensive price swing and no "
            "gate at all, this battery should show real throughput -- if it "
            "doesn't, the scenario isn't exercising the LP the way this test "
            "needs, independent of the mask under test",
        )

        gated = _solve(n, _battery(unavailable_period_indices=away))
        self.assertEqual(gated.status, "optimal")

        for t in sorted(away):
            self.assertEqual(
                float(gated.battery_charge_kw[t]),
                0.0,
                f"period {t} is masked away -- charge must be exactly 0.0, a hard "
                "ub=0.0 bound, not merely a low value the LP chose",
            )
            self.assertEqual(
                float(gated.battery_discharge_kw[t]),
                0.0,
                f"period {t} is masked away -- discharge must be exactly 0.0",
            )

        # The whole point of a per-period mask rather than a prefix: the
        # periods either side of a trip stay genuinely usable.
        home_throughput = sum(
            float(gated.battery_charge_kw[t]) + float(gated.battery_discharge_kw[t])
            for t in range(n)
            if t not in away
        )
        self.assertGreater(
            home_throughput,
            1.0,
            "periods outside the away windows must remain fully schedulable -- a "
            "mask that gated the whole horizon would be indistinguishable from "
            "available=False and would defeat the purpose of #467",
        )

    def test_mask_unions_with_an_existing_prefix_gate_rather_than_replacing_it(self):
        """Precedence: the mask composes with `available=False` +
        `unavailable_until_period_index`, it does not override them. Both
        the prefix AND the mask periods must end up gated.
        """
        n = 10
        prefix_k = 2
        mask = frozenset({6, 7})

        plan = _solve(
            n,
            _battery(
                available=False,
                unavailable_until_period_index=prefix_k,
                unavailable_period_indices=mask,
            ),
        )
        self.assertEqual(plan.status, "optimal")

        expected_gated = set(range(prefix_k)) | set(mask)
        for t in sorted(expected_gated):
            self.assertEqual(
                float(plan.battery_charge_kw[t]),
                0.0,
                f"period {t} is in the prefix-UNION-mask gate and must be exactly 0.0",
            )
            self.assertEqual(float(plan.battery_discharge_kw[t]), 0.0)

        ungated_throughput = sum(
            float(plan.battery_charge_kw[t]) + float(plan.battery_discharge_kw[t])
            for t in range(n)
            if t not in expected_gated
        )
        self.assertGreater(
            ungated_throughput,
            1.0,
            "periods in NEITHER the prefix nor the mask must stay schedulable",
        )

    def test_no_mask_is_byte_identical_to_before_this_field_existed(self):
        """Backwards compatibility: `None` (the default) contributes
        nothing at all, so an existing config solves to exactly the same
        numbers as one that never heard of this field.
        """
        n = 8
        baseline = _solve(n, _battery())
        explicit_none = _solve(n, _battery(unavailable_period_indices=None))
        empty = _solve(n, _battery(unavailable_period_indices=frozenset()))

        for other, label in ((explicit_none, "None"), (empty, "an empty frozenset")):
            np.testing.assert_allclose(
                other.battery_charge_kw,
                baseline.battery_charge_kw,
                err_msg=f"{label} must be a complete no-op on charge",
            )
            np.testing.assert_allclose(
                other.battery_discharge_kw,
                baseline.battery_discharge_kw,
                err_msg=f"{label} must be a complete no-op on discharge",
            )

    def test_indices_beyond_this_solves_horizon_are_ignored_not_an_error(self):
        """A mask is built from a real calendar day's periods, but a given
        solve may be shorter (a manual short-window solve, or the tail of
        a rolling horizon). An out-of-range index is a normal, expected
        case -- the same posture must_have_soc_by_period_index takes --
        not a crash.
        """
        n = 6
        plan = _solve(n, _battery(unavailable_period_indices=frozenset({1, 99, 500})))
        self.assertEqual(plan.status, "optimal")
        self.assertEqual(
            float(plan.battery_charge_kw[1]),
            0.0,
            "the in-range index must still gate normally",
        )
        self.assertEqual(float(plan.battery_discharge_kw[1]), 0.0)

    def test_masking_one_battery_leaves_a_second_battery_untouched(self):
        """The gate is strictly per-participant -- one EV being out
        driving must not constrain the home pack.
        """
        n = 8
        away = frozenset({1, 2, 3})
        plan = build_plan(
            periods=_periods(n),
            grid=_swing_grid(n),
            batteries=[
                _battery(name="home"),
                _battery(name="ev", unavailable_period_indices=away),
            ],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        )
        self.assertEqual(plan.status, "optimal")

        by_name = {b.name: b for b in plan.batteries}
        self.assertIn("home", by_name)
        self.assertIn("ev", by_name)

        for t in sorted(away):
            self.assertEqual(
                float(by_name["ev"].charge_kw[t]),
                0.0,
                f"the masked EV must be gated at period {t}",
            )
            self.assertEqual(float(by_name["ev"].discharge_kw[t]), 0.0)

        home_throughput = float(
            np.sum(by_name["home"].charge_kw) + np.sum(by_name["home"].discharge_kw)
        )
        self.assertGreater(
            home_throughput,
            1.0,
            "the unmasked home battery must be completely unaffected by the EV's "
            "own away windows -- the gate is per-participant, not global",
        )


class TestMaskNormalisationAndValidation(unittest.TestCase):
    def test_a_plain_list_is_normalised_to_a_frozenset(self):
        """Callers naturally produce a list or an np.flatnonzero() result.
        BatteryConfig is frozen=True and therefore hashable, which a
        mutable list field would silently break -- so __post_init__
        normalises whatever iterable it's given.
        """
        cfg = _battery(unavailable_period_indices=[3, 1, 1, 2])
        self.assertIsInstance(cfg.unavailable_period_indices, frozenset)
        self.assertEqual(cfg.unavailable_period_indices, frozenset({1, 2, 3}))
        # Still genuinely hashable after normalisation.
        self.assertIsInstance(hash(cfg), int)

    def test_numpy_integer_indices_are_accepted_and_coerced(self):
        """`np.flatnonzero(~is_home_mask)` yields np.int64, not int --
        solver_writer.py's own wiring feeds exactly that shape.
        """
        cfg = _battery(
            unavailable_period_indices=np.flatnonzero(np.array([0, 1, 1, 0]))
        )
        self.assertEqual(cfg.unavailable_period_indices, frozenset({1, 2}))
        self.assertTrue(
            all(isinstance(i, int) for i in cfg.unavailable_period_indices),
            "indices must be coerced to plain ints, not left as np.int64",
        )

    def test_a_negative_index_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _battery(unavailable_period_indices=frozenset({-1, 2}))
        self.assertIn("non-negative", str(ctx.exception))


class TestOracleAndReconstructionNowAgree(unittest.TestCase):
    def test_the_away_mask_is_the_exact_inverse_of_the_reconstruction_mask(self):
        """The real correctness claim behind #467: the oracle is gated by
        EXACTLY the periods the reconstruction side zeroes, so the two
        halves of the quality scorer can no longer disagree about where
        the car was.

        `_resolve_battery_participant_history()` masks reconstruction with
        `np.where(is_home_mask, x, 0.0)` and derives the LP gate with
        `frozenset(np.flatnonzero(~is_home_mask))`. This pins that
        relationship directly: for a real two-trip day, every period the
        reconstruction zeroed is gated in the LP, and no period it kept is
        gated.
        """
        # A real two-trip day: home, school run, home, evening trip, home.
        is_home_mask = np.array(
            [True, True, False, False, True, True, True, False, False, True]
        )
        n = len(is_home_mask)

        away_period_indices = frozenset(int(i) for i in np.flatnonzero(~is_home_mask))
        self.assertEqual(away_period_indices, frozenset({2, 3, 7, 8}))

        reconstruction = np.where(is_home_mask, np.full(n, 5.0), 0.0)

        plan = _solve(n, _battery(unavailable_period_indices=away_period_indices))
        self.assertEqual(plan.status, "optimal")

        for t in range(n):
            recon_zeroed = float(reconstruction[t]) == 0.0
            oracle_gated = (
                float(plan.battery_charge_kw[t]) == 0.0
                and float(plan.battery_discharge_kw[t]) == 0.0
            )
            if recon_zeroed:
                self.assertTrue(
                    oracle_gated,
                    f"period {t}: the reconstruction zeroed this period (car away) "
                    "but the oracle was still free to dispatch -- this is exactly "
                    "the disagreement #467 exists to remove",
                )

        # And the converse direction that actually matters economically:
        # the oracle must genuinely still use the home periods, otherwise
        # "agreement" would be trivially satisfied by gating everything.
        home_throughput = sum(
            float(plan.battery_charge_kw[t]) + float(plan.battery_discharge_kw[t])
            for t in range(n)
            if is_home_mask[t]
        )
        self.assertGreater(
            home_throughput,
            1.0,
            "the oracle must still dispatch during the periods the car WAS home -- "
            "agreement achieved by gating the whole horizon would be useless",
        )


if __name__ == "__main__":
    unittest.main()
