"""nimbus issue #1015 -- the two counterfactuals now price P2P the same way.

The same P2P program reached the three trajectories EPR divides by each
other through three different mechanisms:

| trajectory            | how P2P entered                              |
|-----------------------|----------------------------------------------|
| `j_ref` (idle)        | **not at all** -- plain spot export price    |
| `j_ach` (what happened) | the real settled lump sum                  |
| `j_star` (oracle)     | a modelled two-tier bonus via `build_plan()` |

**The fix is narrower than the issue first proposed, and correct for a
different reason.** #1015 suggested pricing all three identically. That
would replace `j_ach`'s REAL settled dollars with a model of them, which
is strictly worse -- the achieved trajectory is not a counterfactual and
has actual money attached.

The real principle: **the two counterfactuals share one model; the
actual keeps its real money.** `j_star` already had the model. `j_ref`
had nothing. That omission is the defect, and it is the only thing
changed here.

**Why it matters even though it is currently a no-op.** `j_ref` holds
the battery idle, so its only possible export is solar surplus. On the
reference household in September, solar has finished by ~18:00 and the
committed window is 17:00-24:00, so `j_ref` imports through the whole
window and earns no P2P to be denied. The moment the surplus overlaps
the window -- longer days, a daytime P2P block, a bigger array, a low
evening load -- `j_ref` is scored as earning plain spot on export that
would really have earned the premium. That inflates `j_ref`, which sits
in BOTH the numerator and the denominator of
`EPR = (j_ref - j_ach) / (j_ref - j_star)`, and flatters the Solver.

So these tests come in two halves, and the first is as load-bearing as
the second: the change must be **byte-identical** on the shape the
reference household actually runs today, and must move the number on the
shape that is coming.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.p2p_export import realized_export_bonus_credit
from solver.quality_report import compute_quality_report

N = 24
HOURS = np.ones(N)
START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
STARTS = [START + timedelta(hours=i) for i in range(N)]

CAPACITY = 122.2


def _battery(initial_soc_kwh: float = 60.0) -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=CAPACITY,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=CAPACITY * 0.02,
        max_soc_kwh=CAPACITY,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


def _bonus_price(hours_active: range, premium: float = 0.40) -> np.ndarray:
    arr = np.zeros(N)
    for h in hours_active:
        arr[h] = premium
    return arr


class TestRealizedExportBonusCredit(unittest.TestCase):
    """The accounting helper on its own, where the arithmetic is
    checkable by hand."""

    def _grid(self, price, volume_kwh):
        return GridConfig(
            import_price=np.full(N, 0.30),
            export_price=np.full(N, 0.05),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            export_bonus_price=price,
            export_bonus_volume_kwh=volume_kwh,
        )

    def test_no_bonus_configured_is_zero_not_an_error(self):
        """Callers must not have to branch, so an unconfigured program
        returns 0.0 rather than raising or requiring a guard."""
        plain = GridConfig(
            import_price=np.full(N, 0.30),
            export_price=np.full(N, 0.05),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
        )
        self.assertEqual(
            realized_export_bonus_credit(
                grid_export_kw=np.full(N, 5.0),
                hours=HOURS,
                grid=plain,
                period_starts=STARTS,
            ),
            0.0,
        )

    def test_export_inside_the_window_earns_the_premium(self):
        export = np.zeros(N)
        export[18] = 4.0  # 4 kWh in one hour
        credit = realized_export_bonus_credit(
            grid_export_kw=export,
            hours=HOURS,
            grid=self._grid(_bonus_price(range(17, 24)), 20.0),
            period_starts=STARTS,
        )
        self.assertAlmostEqual(credit, 4.0 * 0.40, places=9)

    def test_export_outside_the_window_earns_nothing(self):
        export = np.zeros(N)
        export[11] = 4.0  # midday, premium is 0 there
        credit = realized_export_bonus_credit(
            grid_export_kw=export,
            hours=HOURS,
            grid=self._grid(_bonus_price(range(17, 24)), 20.0),
            period_starts=STARTS,
        )
        self.assertEqual(credit, 0.0)

    def test_the_volume_cap_binds(self):
        """12 kWh exported against a 5 kWh cap earns the premium on 5."""
        export = np.zeros(N)
        export[18:21] = 4.0  # 12 kWh across three hours
        credit = realized_export_bonus_credit(
            grid_export_kw=export,
            hours=HOURS,
            grid=self._grid(_bonus_price(range(17, 24)), 5.0),
            period_starts=STARTS,
        )
        self.assertAlmostEqual(credit, 5.0 * 0.40, places=9)

    def test_the_highest_premium_periods_are_filled_first(self):
        """Matches the revenue-maximising LP: given a cap smaller than
        the export available, the valuable hours are claimed first."""
        price = np.zeros(N)
        price[18] = 0.10
        price[19] = 0.90
        export = np.zeros(N)
        export[18] = 3.0
        export[19] = 3.0
        credit = realized_export_bonus_credit(
            grid_export_kw=export,
            hours=HOURS,
            grid=self._grid(price, 3.0),
            period_starts=STARTS,
        )
        self.assertAlmostEqual(credit, 3.0 * 0.90, places=9)

    def test_the_cap_resets_each_calendar_day(self):
        """The distinction `add_export_bonus_cumulative_caps()` exists
        to enforce: a nightly program resets, and one global cap would
        let day one spend the whole allocation."""
        n2 = 48
        hours2 = np.ones(n2)
        starts2 = [START + timedelta(hours=i) for i in range(n2)]
        price = np.zeros(n2)
        price[18] = price[42] = 0.40  # 18:00 on each of two days
        export = np.zeros(n2)
        export[18] = export[42] = 10.0

        grid = GridConfig(
            import_price=np.full(n2, 0.30),
            export_price=np.full(n2, 0.05),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            export_bonus_price=price,
            export_bonus_volume_kwh=5.0,
        )
        credit = realized_export_bonus_credit(
            grid_export_kw=export, hours=hours2, grid=grid, period_starts=starts2
        )
        # 5 kWh on each of two days, not 5 kWh total.
        self.assertAlmostEqual(credit, 2 * 5.0 * 0.40, places=9)

    def test_without_timestamps_it_falls_back_to_one_allocation(self):
        """Honest fallback, matching the LP helper's own: with no period
        starts there is no way to know where a day boundary falls."""
        n2 = 48
        hours2 = np.ones(n2)
        price = np.zeros(n2)
        price[18] = price[42] = 0.40
        export = np.zeros(n2)
        export[18] = export[42] = 10.0
        grid = GridConfig(
            import_price=np.full(n2, 0.30),
            export_price=np.full(n2, 0.05),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            export_bonus_price=price,
            export_bonus_volume_kwh=5.0,
        )
        credit = realized_export_bonus_credit(
            grid_export_kw=export, hours=hours2, grid=grid, period_starts=None
        )
        self.assertAlmostEqual(credit, 5.0 * 0.40, places=9)

    def test_it_never_claims_more_than_was_actually_exported(self):
        """A generous cap does not invent revenue on export that did not
        happen -- the retrospective counterpart of the LP's own
        `export_bonus[t] <= grid_export[t]` constraint."""
        export = np.zeros(N)
        export[18] = 1.0
        credit = realized_export_bonus_credit(
            grid_export_kw=export,
            hours=HOURS,
            grid=self._grid(_bonus_price(range(17, 24)), 100.0),
            period_starts=STARTS,
        )
        self.assertAlmostEqual(credit, 1.0 * 0.40, places=9)

    def test_a_zero_cap_earns_nothing(self):
        credit = realized_export_bonus_credit(
            grid_export_kw=np.full(N, 5.0),
            hours=HOURS,
            grid=self._grid(_bonus_price(range(17, 24)), 0.0),
            period_starts=STARTS,
        )
        self.assertEqual(credit, 0.0)


class TestJRefEndToEnd(unittest.TestCase):
    """The half that matters on a real install."""

    def _report(self, *, solar_kw, bonus_hours):
        zero = np.zeros(N)
        residual = GridConfig(
            import_price=np.full(N, 0.30),
            export_price=np.full(N, 0.05),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
        )
        oracle = GridConfig(
            import_price=np.full(N, 0.30),
            export_price=np.full(N, 0.05),
            import_limit_kw=42.0,
            export_limit_kw=42.0,
            export_bonus_price=_bonus_price(bonus_hours),
            export_bonus_volume_kwh=20.0,
        )
        battery = _battery()
        return compute_quality_report(
            periods=PeriodGrid(hours=HOURS, start=START),
            grid_residual=residual,
            grid_oracle=oracle,
            batteries=[battery],
            solar=SolarConfig(forecast_kw=solar_kw),
            load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
            timestamps=STARTS,
            real_p2p_dollars_earned=0.0,
            commanded_charge_kw=[zero],
            commanded_discharge_kw=[zero],
            actual_charge_kw=[zero],
            actual_discharge_kw=[zero],
            final_soc_kwh_actual=[battery.initial_soc_kwh],
        )

    def test_no_overlap_leaves_j_ref_exactly_unchanged(self):
        """The reference household's shape today: solar finishes before
        the committed window opens, so the idle trajectory imports
        through all of it and there is no premium to credit.

        This is the load-bearing no-op claim. If this moves, the change
        is silently rescoring every historical day on every install.
        """
        # Solar only in the morning; committed window is 17:00-24:00.
        solar = np.zeros(N)
        solar[8:14] = 6.0
        with_window = self._report(solar_kw=solar, bonus_hours=range(17, 24))
        without = self._report(solar_kw=solar, bonus_hours=range(0))
        self.assertEqual(with_window.j_ref, without.j_ref)

    def test_overlap_credits_j_ref_and_lowers_it(self):
        """The case that is coming. Surplus inside the window really
        would have earned the premium, so the do-nothing baseline costs
        LESS than plain-spot pricing claimed."""
        solar = np.zeros(N)
        solar[17:21] = 6.0  # 5 kW surplus against a 1 kW load, in-window
        credited = self._report(solar_kw=solar, bonus_hours=range(17, 24))
        plain = self._report(solar_kw=solar, bonus_hours=range(0))

        self.assertLess(
            credited.j_ref,
            plain.j_ref,
            "j_ref must fall once its in-window export earns the premium "
            "it really would have earned -- nimbus #1015",
        )
        # 4 hours x 5 kW surplus = 20 kWh, exactly the configured cap,
        # at a 0.40 premium.
        self.assertAlmostEqual(plain.j_ref - credited.j_ref, 20.0 * 0.40, places=6)

    def test_j_ach_is_untouched_by_the_change(self):
        """The deliberate asymmetry. j_ach keeps its real settled figure
        -- real money beats a model of it -- so crediting j_ref must not
        move it."""
        solar = np.zeros(N)
        solar[17:21] = 6.0
        credited = self._report(solar_kw=solar, bonus_hours=range(17, 24))
        plain = self._report(solar_kw=solar, bonus_hours=range(0))
        self.assertEqual(credited.j_ach, plain.j_ach)

    def test_the_direction_of_the_epr_error_is_the_one_claimed(self):
        """#1015 says the omission FLATTERS the Solver. Worth checking
        rather than asserting, since the fix's whole justification is
        which way the bias runs."""
        solar = np.zeros(N)
        solar[17:21] = 6.0
        credited = self._report(solar_kw=solar, bonus_hours=range(17, 24))
        plain = self._report(solar_kw=solar, bonus_hours=range(0))
        self.assertGreater(
            plain.j_ref,
            credited.j_ref,
            "the un-credited j_ref is the more expensive baseline, which "
            "is what makes the Solver's captured value look larger than "
            "it was",
        )


if __name__ == "__main__":
    unittest.main()
