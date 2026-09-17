"""nimbus #1081: measure whether `j_star` and `j_ach` are even comparable.

`j_star <= j_ach` is supposed to hold by construction, because the oracle's
feasible set contains the achieved trajectory -- the oracle can always, at
worst, replicate what actually happened.

That is an argument about **trajectories**. It only carries over to the
**numbers** if both are priced by the same arithmetic, and they are not:

    j_ach  = evaluate_realized_cost_multi(achieved) - real settled P2P $
    j_star = oracle_plan.total_cost                  <- the LP's objective

An LP objective carries terms an evaluator does not: the soft-SoC penalty,
slack penalties, and the export bonus as a variable the LP *chooses* rather
than a credit allocated after the fact. So a disagreement between the two
paths can manufacture an impossible result out of two individually correct
answers, and nothing has ever checked for it.

The case this was built to settle: 15 Sep 2026 scored `epr_pct` **100.11**
with `regret_dollars` **-0.0214** on a ~$15 objective -- 0.14%, and
structurally impossible. The leading explanation was "probably LP
tolerance". This repo has had several tidy explanations refuted by
measurement recently, so it gets measured instead of asserted.

`p2p_export.realized_export_bonus_credit()`'s own docstring already names
the risk: *"Both must answer the same question the same way, or a
counterfactual scored here is not comparable to one the LP produced."*

These tests pin the instrument, not a threshold. Deliberately no assertion
that the delta is small: that is the open question, and a test that assumed
the answer would be pinning a guess -- the exact failure #1082's own naive
timestamp test made hours earlier.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
import pytest
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.quality_report import compute_quality_report

N = 24
HOURS = np.full(N, 1.0)
START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
STARTS = [START + timedelta(hours=i) for i in range(N)]


def _battery():
    return BatteryConfig(
        name="home",
        capacity_kwh=100.0,
        initial_soc_kwh=50.0,
        min_soc_kwh=2.0,
        max_soc_kwh=100.0,
        max_charge_kw=20.0,
        max_discharge_kw=20.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=np.full(N, 0.01),
        salvage_value=0.0,
    )


def _report(*, bonus_price=None, bonus_volume=None, real_p2p=0.0):
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
        export_bonus_price=bonus_price,
        export_bonus_volume_kwh=bonus_volume,
    )
    solar = np.zeros(N)
    solar[8:15] = 8.0
    battery = _battery()
    return compute_quality_report(
        periods=PeriodGrid(hours=HOURS, start=START),
        grid_residual=residual,
        grid_oracle=oracle,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=solar),
        load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
        timestamps=STARTS,
        real_p2p_dollars_earned=real_p2p,
        commanded_charge_kw=[zero],
        commanded_discharge_kw=[zero],
        actual_charge_kw=[zero],
        actual_discharge_kw=[zero],
        final_soc_kwh_actual=[battery.initial_soc_kwh],
    )


class TestTheIdentityHolds(unittest.TestCase):
    def test_delta_is_exactly_j_star_minus_the_evaluator_repricing(self):
        """The whole point of the field is that a reader can check the
        subtraction themselves. If these ever disagree the number is
        worse than useless, because it looks authoritative."""
        r = _report()
        self.assertAlmostEqual(
            r.j_star_path_delta,
            round(r.j_star - r.j_star_evaluator, 4),
            places=4,
        )

    def test_both_fields_are_real_numbers_not_defaults(self):
        """They default to 0.0 so every construction site predating them
        keeps working -- which means a wiring failure would look exactly
        like a perfect agreement. #1016 is the precedent: its tests
        asserted values that were always set correctly while the thing
        they gated had silently stopped running."""
        r = _report()
        self.assertIsInstance(r.j_star_evaluator, float)
        self.assertNotEqual(
            r.j_star_evaluator,
            0.0,
            "j_star_evaluator is exactly 0.0, which for this scenario means "
            "it was never computed -- a default masquerading as agreement",
        )


class TestItSeesABonusModelDisagreement(unittest.TestCase):
    """The specific asymmetry #1081 suspects: the LP CHOOSES which periods
    claim the capped bonus volume, while the evaluator ALLOCATES it after
    the fact. Both are defensible; they are only comparable if they land on
    the same number."""

    def test_a_bonus_priced_oracle_still_produces_a_finite_delta(self):
        bonus = np.zeros(N)
        bonus[17:24] = 0.20
        r = _report(bonus_price=bonus, bonus_volume=20.0, real_p2p=3.0)
        self.assertTrue(
            np.isfinite(r.j_star_path_delta),
            "a bonus-priced oracle must still reprice through the evaluator "
            "path -- an inf/nan here means the two paths cannot be compared "
            "at all, which is a stronger finding than a disagreement",
        )
        self.assertAlmostEqual(
            r.j_star_path_delta,
            round(r.j_star - r.j_star_evaluator, 4),
            places=4,
        )

    @pytest.mark.xfail(
        reason=(
            "nimbus #1081, MEASURED 2026-09-17: the LP objective and the "
            "evaluator disagree by $0.4560 on a ~$3 objective (15%) for the "
            "IDENTICAL trajectory with no export bonus in play at all. "
            "j_star=-2.9680 vs j_star_evaluator=-3.4240.\n\n"
            "This refutes the issue's own leading hypothesis. It is not the "
            "bonus allocation (this scenario has no bonus), and it is not "
            "the charge/discharge costs (the delta is invariant to both -- "
            "setting either to zero moves j_star and the evaluator by the "
            "same amount). The delta is independent of solar (0.4560 at 0, "
            "2 and 8 kW) and scales with starting SoC (0.2830 at 10 kWh, "
            "0.4560 at 50, 0.8360 at 90).\n\n"
            "Forcing the oracle's soft-SoC penalty to zero -- which "
            "compute_quality_report() already does per #586 when a day "
            "starts below floor -- roughly HALVES it (0.4560 -> 0.2165). "
            "So the penalty, a modelling device j_ach never pays, is a "
            "major contributor but not the whole gap; at least one more "
            "term is unaccounted for.\n\n"
            "Direction matters: an inflated j_star makes the oracle look "
            "WORSE, which shrinks regret and inflates EPR -- the same "
            "direction as the 100.11% / -$0.0214 that opened this issue."
        ),
        strict=True,
    )
    def test_an_unbonused_oracle_agrees_closely(self):
        """With no bonus configured, the LP has no bonus variable to choose
        and the evaluator has no volume to allocate, so the only remaining
        difference between the paths is the LP's own penalty terms. This is
        the control: if THIS disagrees materially, the problem is not the
        bonus model at all.

        It does, and it is not. That is the finding -- this control was
        written expecting to pass.
        """
        r = _report()
        self.assertLess(
            abs(r.j_star_path_delta),
            0.01,
            "with no export bonus in play the LP objective and the evaluator "
            "should price the identical trajectory identically; a gap here "
            "points at the penalty terms rather than the bonus allocation\n"
            f"  j_star={r.j_star}  j_star_evaluator={r.j_star_evaluator}",
        )


if __name__ == "__main__":
    unittest.main()
