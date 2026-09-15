"""Regression test for nimbus issue #953 (Mark Purcell, from the 48-hour
IV&V pass #950): a latent `lb > ub` conflict in the discharge-variable
construction when the price-spike override and the per-period
availability mask coincide on period 0.

Before the fix, `discharge_vars`' bounds were built from two conditions
that did not consult each other:

    lb = spike_override_value if spike_active_now and t == 0 else 0.0
    ub = 0.0 if t in gated_periods else (...)

`ub` honours the gate; `lb` never looked at it. So a battery carrying
BOTH a positive `spike_override_discharge_kw` and period 0 inside
`unavailable_period_indices` built a positive floor against a zero
ceiling, which `add_variable` rejects outright:

    ValueError: Variable 'battery_discharge_home_0' has lb=5.0 > ub=0.0

A hard failure of the whole solve, then, not a wrong number -- the plan
does not degrade, it does not arrive.

`charge_vars` is deliberately NOT the template for the fix. Charge has no
`lb` override at all, because a spike override raises a *minimum
discharge* and never a minimum charge; the two constructions are
legitimately different in shape, and only one of them had been taught
about the mask. ORing the spike condition into `ub` -- the remedy first
suggested on the issue -- would have been a no-op, since `ub` is already
`0.0` in exactly the conflicting case.

**The gate wins over the override, and that is the design decision this
file pins.** An unavailable battery cannot discharge whatever the price
is doing, so "the car is out and prices spiked" is a well-defined
no-discharge period rather than a misconfiguration to refuse. That is
also what #467's own design note says the mask does -- it *"composes as a
UNION with the existing gates"* -- so rejecting the pair in
`BatteryConfig.__post_init__` would have made this one combination an
exception to the rule the rest of the file follows.

The combination is unreachable today: the spike override is set only on
the live-forward "home" construction (`solver_writer.py` ~13857) and the
mask only on the oracle re-solve's (~6523-6582). This file exists so that
extending either mechanism cannot silently reintroduce the conflict --
converting "latent and unreachable" into "guarded and executable", which
is the actual risk #953 identifies.
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

# Expensive right now, cheap later: the LP has every economic reason to
# discharge at t=0 and none to hold back, so a discharge of exactly 0.0
# there is 0.0 because it was GATED, never because it was unattractive.
_DEAR = 0.60
_CHEAP = 0.05

_SPIKE_KW = 5.0


def _periods(n: int) -> PeriodGrid:
    return PeriodGrid(
        hours=np.array([1.0] * n), start=datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    )


def _dear_now_grid(n: int) -> GridConfig:
    import_price = np.array([_DEAR] + [_CHEAP] * (n - 1))
    return GridConfig(
        import_price=import_price,
        export_price=import_price - 0.02,
        import_limit_kw=200.0,
        export_limit_kw=200.0,
    )


def _battery(**overrides) -> BatteryConfig:
    kwargs = {
        "name": "home",
        "capacity_kwh": 40.0,
        "initial_soc_kwh": 30.0,
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
        grid=_dear_now_grid(n),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
    )


class TestSpikeOverrideAndAvailabilityGateCompose(unittest.TestCase):
    def test_the_override_alone_really_does_pin_discharge(self):
        """Sanity check, and the reason the gated assertion below means
        anything: without a gate, the override must genuinely force
        discharge to exactly its value at t=0. If this ever stops being
        true, the conflict test would pass for the wrong reason -- there
        would simply be no positive lower bound left to conflict with.
        """
        n = 8
        plan = _solve(n, _battery(spike_override_discharge_kw=_SPIKE_KW))

        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(
            float(plan.battery_discharge_kw[0]),
            _SPIKE_KW,
            places=6,
            msg="the price-spike override pins discharge at t=0 to exactly its "
            "own value (lb == ub), so anything else means the override is no "
            "longer the mechanism this test thinks it is",
        )

    def test_a_gated_period_zero_with_an_active_override_still_solves(self):
        """The #953 failure scenario itself: both mechanisms on the same
        battery, both landing on period 0.

        Before the fix this did not produce a wrong plan -- it produced
        `lb=5.0, ub=0.0` and a `ValueError` out of `add_variable`, so the
        assertion that matters first is simply that a plan comes back at
        all.
        """
        n = 8
        plan = _solve(
            n,
            _battery(
                spike_override_discharge_kw=_SPIKE_KW,
                unavailable_period_indices=frozenset({0}),
            ),
        )

        self.assertEqual(
            plan.status,
            "optimal",
            "a battery with both a positive spike override and period 0 masked "
            "away must produce a well-defined plan -- before #953 this handed "
            "HiGHS lb=5.0 against ub=0.0",
        )
        self.assertEqual(
            float(plan.battery_discharge_kw[0]),
            0.0,
            "the availability gate WINS over the spike override: a battery that "
            "is away cannot discharge whatever the price is doing",
        )
        self.assertEqual(
            float(plan.battery_charge_kw[0]),
            0.0,
            "charge at a gated period was already correct and must stay so -- "
            "this fix touches only discharge's lower bound",
        )

    def test_gating_a_later_period_leaves_the_override_intact(self):
        """The fix is scoped to the period the gate and the override
        actually share. A mask elsewhere in the horizon must not quietly
        disarm an override at t=0 -- that would be a real behaviour
        regression dressed up as a bug fix.
        """
        n = 8
        plan = _solve(
            n,
            _battery(
                spike_override_discharge_kw=_SPIKE_KW,
                unavailable_period_indices=frozenset({3, 4}),
            ),
        )

        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(
            float(plan.battery_discharge_kw[0]),
            _SPIKE_KW,
            places=6,
            msg="period 0 is not gated here, so the override must still pin it",
        )
        for t in (3, 4):
            self.assertEqual(
                float(plan.battery_discharge_kw[t]),
                0.0,
                f"period {t} is masked away -- discharge must be exactly 0.0",
            )

    def test_the_779_prefix_gate_cannot_reach_the_conflict(self):
        """`gated_periods` unions three sources, not just #467's mask:
        `available=False` gates the whole horizon, and #779's
        `unavailable_until_period_index=k` gates `[0, k)`. Both cover
        period 0, so on the face of it both could reach the same
        conflict. Neither can, and the reason is worth pinning rather
        than re-deriving.

        **#779's prefix is a refinement of `available=False`, not an
        independent gate.** It is read only inside the `elif` under
        `if b.available: gated_periods = frozenset()` -- so on an
        AVAILABLE battery it is inert and gates nothing, which is what
        this test's first half asserts. And on an unavailable one,
        `spike_active_now` requires `b.available`, so the override never
        arms in the first place.

        That leaves #467's per-period mask as the only source that can
        gate period 0 while the override is live, which is precisely the
        scope #953 identified.
        """
        n = 8

        # Available: the prefix index is inert, so the override stands.
        available = _solve(
            n,
            _battery(
                spike_override_discharge_kw=_SPIKE_KW,
                unavailable_until_period_index=2,
            ),
        )
        self.assertEqual(available.status, "optimal")
        self.assertAlmostEqual(
            float(available.battery_discharge_kw[0]),
            _SPIKE_KW,
            places=6,
            msg="`unavailable_until_period_index` is only consulted when "
            "`available` is False -- on an available battery it must gate "
            "nothing, leaving the override to pin t=0",
        )

        # Unavailable: the prefix gates [0, 2) and the override is never
        # armed, so there is no lower bound left to conflict with.
        unavailable = _solve(
            n,
            _battery(
                available=False,
                spike_override_discharge_kw=_SPIKE_KW,
                unavailable_until_period_index=2,
            ),
        )
        self.assertEqual(unavailable.status, "optimal")
        for t in (0, 1):
            self.assertEqual(
                float(unavailable.battery_discharge_kw[t]),
                0.0,
                f"period {t} is inside the #779 prefix gate -- discharge must be "
                "exactly 0.0",
            )

    def test_an_unavailable_battery_never_arms_the_override_at_all(self):
        """Documents the second, independent reason `available=False` was
        never the reachable half of #953: `spike_active_now` requires
        `b.available`, so the override is not merely overridden here --
        it is never constructed. Worth pinning so a future refactor that
        drops that term from `spike_active_now` fails loudly rather than
        reintroducing the conflict through the whole-horizon gate.
        """
        n = 8
        plan = _solve(
            n,
            _battery(available=False, spike_override_discharge_kw=_SPIKE_KW),
        )

        self.assertEqual(plan.status, "optimal")
        self.assertEqual(float(plan.battery_discharge_kw[0]), 0.0)
        self.assertEqual(float(plan.battery_charge_kw[0]), 0.0)


if __name__ == "__main__":
    unittest.main()
