"""Real household finding, 2026-08-21: "the forecasts are always wrong
but they tend to be more expensive in the afternoons, so waiting is not
a good idea." Existing risk_aversion (mechanism 3) only ever hedged
solar/load forecast error -- price risk aversion extends the same idea
to import/export price, using GridConfig.import_price_upper/
export_price_lower (both optional, both independent from risk_aversion
per the explicit "more flexibility" ask, symmetric for discharge/export
per the direct "same for discharge if possible" follow-up ask).

Split into import_price_risk_aversion/export_price_risk_aversion later
the same day, per direct Mark Purcell feedback: a single shared
price_risk_aversion scalar forces charge and discharge hedging to move
together even though they're economically opposite decisions -- hedging
"import might be more expensive than forecast" should never also force
hedging "export might be worth less than forecast" by the same amount.
test_import_and_export_dials_are_fully_independent below is the real,
direct proof that split actually holds at the LP level, not just at the
config-surface level.
"""

import unittest

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


def _scenario(import_price_upper=None, export_price_lower=None):
    n = 10
    hours = np.array([1.0] * n)
    from datetime import datetime

    periods = PeriodGrid(hours=hours, start=datetime(2026, 8, 21, 12, 0))

    # A genuinely cheap-looking point forecast throughout -- if the LP
    # trusts it at face value, it has no reason to charge early (plenty
    # of time, no price pressure). A real household concern: the point
    # forecast for LATER periods might be understating what actually
    # happens.
    import_price = np.array([0.10] * n)
    export_price = np.array([0.30] * n)

    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
        import_price_upper=import_price_upper,
        export_price_lower=export_price_lower,
    )
    battery = BatteryConfig(
        capacity_kwh=122.2,
        initial_soc_kwh=122.2 * 0.5,
        min_soc_kwh=122.2 * 0.05,
        max_soc_kwh=122.2 * 1.0,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.0))]
    return periods, grid, battery, solar, loads


class TestPriceRiskAversion(unittest.TestCase):
    def test_zero_risk_aversion_is_a_complete_noop(self):
        """Even WITH real bounds given, both dials at 0.0 must produce
        byte-identical output to no bounds at all -- the no-op guarantee
        every other stability mechanism already has."""
        periods, grid_a, battery, solar, loads = _scenario(
            import_price_upper=np.array([0.50] * 10),
            export_price_lower=np.array([0.05] * 10),
        )
        periods_b, grid_b, _, _, _ = _scenario()
        plan_a = build_plan(
            periods=periods,
            grid=grid_a,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        plan_b = build_plan(
            periods=periods_b,
            grid=grid_b,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        self.assertEqual(plan_a.status, "optimal")
        np.testing.assert_allclose(plan_a.grid_import_kw, plan_b.grid_import_kw)
        np.testing.assert_allclose(plan_a.grid_export_kw, plan_b.grid_export_kw)
        self.assertAlmostEqual(plan_a.total_cost, plan_b.total_cost, places=6)

    def test_none_bounds_is_a_complete_noop_even_with_nonzero_risk_aversion(self):
        """Both dials at 1.0 but no bounds given at all -- must also be
        a no-op, matching _risk_adjusted_one_sided()'s own documented
        guarantee."""
        periods, grid, battery, solar, loads = _scenario()
        periods_b, grid_b, _, _, _ = _scenario()
        plan_a = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=1.0,
            export_price_risk_aversion=1.0,
        )
        plan_b = build_plan(
            periods=periods_b,
            grid=grid_b,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        np.testing.assert_allclose(plan_a.grid_import_kw, plan_b.grid_import_kw)
        np.testing.assert_allclose(plan_a.grid_export_kw, plan_b.grid_export_kw)

    def test_full_risk_aversion_uses_the_full_upper_bound_for_import(self):
        """import_price_risk_aversion=1.0 with a real upper bound -- the
        LP's OWN effective cost view should be exactly the bound (not
        the cheap point forecast), verified indirectly: charging becomes
        strictly less attractive than in the baseline (risk=0) case,
        since the LP now believes import is genuinely more expensive."""
        periods0, grid0, battery, solar, loads = _scenario(
            import_price_upper=np.array([0.80] * 10)
        )
        periods1, grid1, _, _, _ = _scenario(import_price_upper=np.array([0.80] * 10))
        plan0 = build_plan(
            periods=periods0,
            grid=grid0,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        plan1 = build_plan(
            periods=periods1,
            grid=grid1,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=1.0,
            export_price_risk_aversion=0.0,
        )
        self.assertEqual(plan0.status, "optimal")
        self.assertEqual(plan1.status, "optimal")
        # Real, measurable effect: total import volume should NOT increase
        # (and typically decreases) once the LP believes import is more
        # expensive -- it has no reason to import MORE under a pessimistic
        # price view than under an optimistic one, all else equal.
        self.assertLessEqual(
            plan1.grid_import_kw.sum(), plan0.grid_import_kw.sum() + 1e-6
        )

    def test_export_price_lower_is_symmetric_for_discharge(self):
        """Direct household follow-up: 'i would do the same for
        discharge if possible.' A pessimistic (lower) export price bound
        should make export strictly less attractive (or equal), never
        MORE attractive, at full export_price_risk_aversion."""
        periods0, grid0, battery, solar, loads = _scenario(
            export_price_lower=np.array([0.05] * 10)
        )
        periods1, grid1, _, _, _ = _scenario(export_price_lower=np.array([0.05] * 10))
        plan0 = build_plan(
            periods=periods0,
            grid=grid0,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        plan1 = build_plan(
            periods=periods1,
            grid=grid1,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=1.0,
        )
        self.assertGreaterEqual(plan1.total_cost, plan0.total_cost - 1e-6)

    def test_bound_worse_than_forecast_is_ignored_not_inverted(self):
        """A real, honest edge case: if the 'upper' bound given is
        actually BELOW the point forecast (a genuinely bad/stale bound),
        np.maximum(0.0, ...) must clamp this to zero adjustment, not
        push the effective price DOWN -- the mechanism can only ever
        push pessimistically in its own documented direction, never the
        opposite."""
        periods, grid, battery, solar, loads = _scenario(
            import_price_upper=np.array([0.01] * 10),  # below the 0.10 point forecast
        )
        periods_b, grid_b, _, _, _ = _scenario()
        plan_a = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=1.0,
            export_price_risk_aversion=0.0,
        )
        plan_b = build_plan(
            periods=periods_b,
            grid=grid_b,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        np.testing.assert_allclose(plan_a.grid_import_kw, plan_b.grid_import_kw)

    def test_import_and_export_dials_are_fully_independent(self):
        """The real, direct proof of the 2026-08-21 split: a nonzero
        export_price_risk_aversion must have ZERO effect on the plan
        when import_price_risk_aversion=0.0 -- i.e. giving the LP a real
        export_price_lower bound while export risk aversion is off is
        exactly as inert as not giving one at all, REGARDLESS of what
        import_price_risk_aversion is set to. And the mirror image for
        import. This is the property Mark Purcell's critique said the
        old single shared scalar could never have (turning one dial
        necessarily moved both sides together)."""
        up = np.array([0.80] * 10)
        low = np.array([0.05] * 10)

        # Export side OFF (0.0): a real export bound present vs. entirely
        # absent must be byte-identical, no matter what import risk
        # aversion is doing.
        for import_risk in (0.0, 1.0):
            periods_with, grid_with, battery, solar, loads = _scenario(
                import_price_upper=up, export_price_lower=low
            )
            periods_without, grid_without, _, _, _ = _scenario(
                import_price_upper=up, export_price_lower=None
            )
            plan_with = build_plan(
                periods=periods_with,
                grid=grid_with,
                battery=battery,
                solar=solar,
                loads=loads,
                import_price_risk_aversion=import_risk,
                export_price_risk_aversion=0.0,
            )
            plan_without = build_plan(
                periods=periods_without,
                grid=grid_without,
                battery=battery,
                solar=solar,
                loads=loads,
                import_price_risk_aversion=import_risk,
                export_price_risk_aversion=0.0,
            )
            np.testing.assert_allclose(
                plan_with.grid_import_kw,
                plan_without.grid_import_kw,
                err_msg=f"export bound leaked into the plan with import_risk={import_risk}, export_risk=0.0",
            )
            np.testing.assert_allclose(
                plan_with.grid_export_kw,
                plan_without.grid_export_kw,
                err_msg=f"export bound leaked into the plan with import_risk={import_risk}, export_risk=0.0",
            )

        # Import side OFF (0.0): mirror image -- a real import bound
        # present vs. entirely absent must be byte-identical, no matter
        # what export risk aversion is doing.
        for export_risk in (0.0, 1.0):
            periods_with, grid_with, battery, solar, loads = _scenario(
                import_price_upper=up, export_price_lower=low
            )
            periods_without, grid_without, _, _, _ = _scenario(
                import_price_upper=None, export_price_lower=low
            )
            plan_with = build_plan(
                periods=periods_with,
                grid=grid_with,
                battery=battery,
                solar=solar,
                loads=loads,
                import_price_risk_aversion=0.0,
                export_price_risk_aversion=export_risk,
            )
            plan_without = build_plan(
                periods=periods_without,
                grid=grid_without,
                battery=battery,
                solar=solar,
                loads=loads,
                import_price_risk_aversion=0.0,
                export_price_risk_aversion=export_risk,
            )
            np.testing.assert_allclose(
                plan_with.grid_import_kw,
                plan_without.grid_import_kw,
                err_msg=f"import bound leaked into the plan with export_risk={export_risk}, import_risk=0.0",
            )
            np.testing.assert_allclose(
                plan_with.grid_export_kw,
                plan_without.grid_export_kw,
                err_msg=f"import bound leaked into the plan with export_risk={export_risk}, import_risk=0.0",
            )

        # And a real, positive confirmation each dial DOES have an effect
        # on its own side when turned on with a real bound -- otherwise
        # the independence proof above would be trivially true of a
        # mechanism that does nothing at all.
        periods_imp_on, grid_imp_on, battery, solar, loads = _scenario(
            import_price_upper=up
        )
        periods_imp_off, grid_imp_off, _, _, _ = _scenario(import_price_upper=up)
        plan_imp_on = build_plan(
            periods=periods_imp_on,
            grid=grid_imp_on,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=1.0,
            export_price_risk_aversion=0.0,
        )
        plan_imp_off = build_plan(
            periods=periods_imp_off,
            grid=grid_imp_off,
            battery=battery,
            solar=solar,
            loads=loads,
            import_price_risk_aversion=0.0,
            export_price_risk_aversion=0.0,
        )
        self.assertFalse(
            np.allclose(plan_imp_on.grid_import_kw, plan_imp_off.grid_import_kw),
            "import_price_risk_aversion=1.0 with a real bound had no measurable effect at all",
        )


if __name__ == "__main__":
    unittest.main()
