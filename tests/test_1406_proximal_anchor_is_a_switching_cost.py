"""nimbus issue #1406 — the proximal anchor is a SWITCHING cost, not an
energy cost, so it must not be scaled by a period's duration.

## The defect this pins

`_add_proximal_penalty()` charged `proximal_weight * hours[new_idx]`. Its job,
stated in `network.py`'s own module docstring, is to *"tip the LP toward the one
closer to the previous plan instead of an arbitrary vertex of the tie"* when two
solutions are economically **tied**. A tie-break is not an energy cost, and
scaling it by period duration made it weakest on the shortest periods — which are
`period[0]` and `[1]`, the only periods that ever become a real inverter command.

Measured on the reference household, 2026-09-28, 10:00–13:00 AEST:
`sensor.nimbus_solver_battery_forecast`'s state (period[0] net kW) snapped to
exactly `0.000` on **18 of 400 samples** and back to the full −40 kW clamp, e.g.
`+0.000 → −40.000` at 11:15:21 and back sixteen seconds later. **Every zero
coincided to the second with a HEALTHY solve** (`solve_seconds` 1.13–1.74 s), so
these were successive solves of a near-identical problem landing on different
vertices — not failed solves, not stale writes. At `proximal_weight` 0.045 on a
1-minute period the anchor was `0.045 * (1/60) * 40 = $0.030` against walking away
from what the hardware was already doing. Downstream: **26 inverter command writes
in three hours**, several 15–30 s apart.

## Why the SMOOTHNESS penalty deliberately keeps its `hours[t]` factor

This is asserted here too, because the symmetry is inviting and acting on it would
be wrong — I nearly did while filing #1406.

For `_add_intraplan_smoothness_penalty`, the gain from choosing a jagged shape is
energy arbitrage, `price_spread * kW * hours`. `hours` appears on **both** sides
and cancels, leaving a threshold `spread > 4 * smoothness_weight` that is uniform
across tiers. The duration scaling there is load-bearing and correct.

The proximal case has no such cancellation: between two solves ~17 s apart the
problem has barely changed, so there is no `hours`-scaled gain on the other side
of the ledger at all.

## Why the magnitude on period[0] matters at all

Mechanism 2's HARD cross-solve period-0 cap (`max_rate_kw`) is what
`network.py`'s docstring calls the thing that *"actually protects the real
inverter from being commanded to swing from e.g. −40kW to +40kW between two
consecutive dispatch cycles"* — and it is deliberately **off**:
`solver_writer.py` declines it because a hard cap would smear the genuine 5pm P2P
transition. That leaves this soft anchor as the only thing between the LP and
flip-flopping the live command.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401 -- sys.path setup side effect
import numpy as np
from solver.lp import LPProblem
from solver.network import (
    _add_intraplan_smoothness_penalty,
    _add_proximal_penalty,
)

# A tiered grid of the shape the reference install actually publishes: two
# 1-minute periods, then 5-minute, then 30-minute. Taken from a live read of
# sensor.nimbus_solver_battery_forecast rather than invented.
TIERED_HOURS = np.array([1 / 60, 1 / 60, 5 / 60, 5 / 60, 0.5, 0.5], dtype=np.float64)
PREVIOUS = np.array([-40.0, -40.0, -40.0, -40.0, -40.0, -40.0], dtype=np.float64)
WEIGHT = 0.045  # the reference install's live proximal_weight


def _build_proximal(hours: np.ndarray) -> tuple[LPProblem, dict[str, float]]:
    """Run the penalty over `hours` and return the problem plus its costs."""
    p = LPProblem()
    names = [f"batt_{t}" for t in range(len(hours))]
    for n in names:
        p.add_variable(n, lb=-100.0, ub=100.0)
    _add_proximal_penalty(
        p,
        names,
        "batt",
        {t: t for t in range(len(hours))},
        PREVIOUS,
        WEIGHT,
    )
    return p, dict(p._cost)


class TestTheAnchorDoesNotShrinkWithPeriodLength(unittest.TestCase):
    """The core of #1406, asserted on the costs themselves."""

    def test_every_period_gets_the_same_anchor_cost(self):
        _p, costs = _build_proximal(TIERED_HOURS)
        prox = {k: v for k, v in costs.items() if k.startswith("prox_")}
        self.assertEqual(
            len(prox),
            2 * len(TIERED_HOURS),
            "expected one dev_pos and one dev_neg per aligned period",
        )
        distinct = sorted(set(prox.values()))
        self.assertEqual(
            distinct,
            [WEIGHT],
            "the proximal anchor must cost the same per kW of deviation in every "
            f"period regardless of its duration; got {distinct} across a tiered "
            "grid of 1-minute, 5-minute and 30-minute periods. If this is a list "
            "of several values, the hours[] scaling is back -- see #1406.",
        )

    def test_a_one_minute_period_is_not_thirty_times_cheaper_than_a_half_hour(self):
        """The specific ratio the defect produced, named so a regression is
        recognisable rather than merely failing."""
        _p, costs = _build_proximal(TIERED_HOURS)
        one_min = costs["prox_pos_batt_0"]
        half_hour = costs["prox_pos_batt_4"]
        self.assertEqual(
            one_min,
            half_hour,
            f"period[0] (1 minute) anchored at {one_min} against a 30-minute "
            f"period's {half_hour} -- a ratio of "
            f"{(half_hour / one_min) if one_min else float('inf'):.1f}x. Before "
            "#1406 this was 30x, and period[0] is the only period that becomes a "
            "real inverter command.",
        )

    def test_the_anchor_is_independent_of_the_hours_array_entirely(self):
        """Two grids whose durations differ by 30x must produce identical costs."""
        _p1, costs_fine = _build_proximal(np.full(6, 1 / 60, dtype=np.float64))
        _p2, costs_coarse = _build_proximal(np.full(6, 0.5, dtype=np.float64))
        self.assertEqual(
            {k: v for k, v in costs_fine.items() if k.startswith("prox_")},
            {k: v for k, v in costs_coarse.items() if k.startswith("prox_")},
            "an all-1-minute grid and an all-30-minute grid must anchor "
            "identically; if they differ, duration is still leaking in.",
        )


class TestSmoothnessKeepsItsDurationScalingDeliberately(unittest.TestCase):
    """The counterpart, asserted so the symmetry is not 'fixed' later.

    For smoothness the gain is energy arbitrage (`spread * kW * hours`), so
    `hours` cancels and the threshold `spread > 4 * weight` is already uniform
    across tiers. Removing the factor here would break a correct derivation.
    """

    def test_smoothness_cost_still_scales_with_the_period_duration(self):
        p = LPProblem()
        names = [f"s_{t}" for t in range(len(TIERED_HOURS))]
        for n in names:
            p.add_variable(n, lb=-100.0, ub=100.0)
        _add_intraplan_smoothness_penalty(
            p, names, "s", len(TIERED_HOURS), TIERED_HOURS, 0.010
        )
        costs = {k: v for k, v in p._cost.items() if k.startswith("smooth_")}
        self.assertTrue(costs, "the smoothness penalty added no costed variables")
        # t starts at 1, so hours[1] (1 min) and hours[4] (30 min) are both used.
        self.assertAlmostEqual(costs["smooth_pos_s_1"], 0.010 * (1 / 60), places=12)
        self.assertAlmostEqual(costs["smooth_pos_s_4"], 0.010 * 0.5, places=12)
        self.assertNotEqual(
            costs["smooth_pos_s_1"],
            costs["smooth_pos_s_4"],
            "smoothness is an ENERGY cost and its hours[t] factor is correct -- "
            "the gain it competes against scales with hours too, so the two "
            "cancel and the threshold is already uniform across tiers. #1406 "
            "changed the PROXIMAL penalty only, on purpose.",
        )


class TestTheNoOpGuardsStillHold(unittest.TestCase):
    """Dropping a parameter must not have disturbed the cheap-exit paths."""

    def test_zero_weight_adds_nothing(self):
        p = LPProblem()
        p.add_variable("batt_0", lb=-100.0, ub=100.0)
        _add_proximal_penalty(p, ["batt_0"], "batt", {0: 0}, PREVIOUS, 0.0)
        self.assertEqual(
            [k for k in p._cost if k.startswith("prox_")],
            [],
            "proximal_weight=0.0 must add no variables at all, not zero-cost ones",
        )

    def test_empty_alignment_adds_nothing(self):
        p = LPProblem()
        p.add_variable("batt_0", lb=-100.0, ub=100.0)
        _add_proximal_penalty(p, ["batt_0"], "batt", {}, PREVIOUS, WEIGHT)
        self.assertEqual([k for k in p._cost if k.startswith("prox_")], [])

    def test_no_previous_values_adds_nothing(self):
        p = LPProblem()
        p.add_variable("batt_0", lb=-100.0, ub=100.0)
        _add_proximal_penalty(p, ["batt_0"], "batt", {0: 0}, None, WEIGHT)
        self.assertEqual([k for k in p._cost if k.startswith("prox_")], [])


if __name__ == "__main__":
    unittest.main()
