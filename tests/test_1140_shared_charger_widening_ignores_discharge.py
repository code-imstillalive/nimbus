"""IV&V finding (5035f90..c881b52 pass, 2026-09-19): #1109's own shared-
charger cap widening ignores discharge, but the LP constraint it exists
to widen against sums charge+discharge.

`_widen_shared_charger_cap_to_achieved()` (`solver_writer.py:13861`)
computes "the largest simultaneous draw the group actually made" as the
per-period sum of `charge_kw` alone across group members -- `discharge_kw`
is discarded (`_discharge_kw` in the loop at line 13897).

But the live LP constraint this cap widens against
(`network.py:3365-3372`, `shared_charger_{group}_t{t}`) sums BOTH
`charge_vars` and `discharge_vars` per member -- physically correct,
since a shared charger's throughput capacity is bounded by total power
flow through the one physical connector regardless of direction (a
bidirectional/V2H participant discharging through the shared charger
draws from the same shared capacity as one charging).

So if a group member genuinely discharged through the shared charger
during a scored day, the true combined draw the LP constraint models can
exceed what `achieved_peak` measures (charge only) -- the widened cap can
remain narrower than the real combined draw, reintroducing exactly the
#956 failure mode #1109 exists to prevent, for the discharge case.

This test constructs a scenario where charge alone never exceeds the
configured cap, but charge+discharge does -- so a correct widening must
move the cap, and the current (charge-only) implementation does not.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver import elements
from solver_inputs import battery_participants as battery_participants_inputs


def _cfg(name, group, cap, *, max_charge_kw=15.0, max_discharge_kw=15.0):
    return elements.BatteryConfig(
        name=name,
        capacity_kwh=60.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=0.0,
        max_soc_kwh=60.0,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_efficiency=0.97,
        discharge_efficiency=0.97,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
        shared_charger_group=group,
        shared_charger_max_kw=cap,
    )


def _widen(rows):
    return battery_participants_inputs._widen_shared_charger_cap_to_achieved(rows)


class TestDischargeIsInvisibleToTheWidening(unittest.TestCase):
    # Was xfail(strict=True) pinning nimbus issue #1140 while the bug was
    # live: _widen_shared_charger_cap_to_achieved() summed only charge_kw
    # per group member and discarded discharge_kw, while the LP constraint
    # it widens against (network.py, shared_charger_{group}_t{t}) sums
    # charge+discharge together. Fixed -- the marker is gone so this now
    # guards the fix instead of documenting the defect.
    def test_the_cap_widens_to_the_combined_charge_and_discharge_peak(self):
        # Configured cap 25.0. In period 0: ev1 charges 10.0 (well under
        # the cap alone), ev2 DISCHARGES 20.0 through the same shared
        # charger (e.g. V2H). The real combined draw the LP constraint
        # models is 10.0 + 20.0 = 30.0 -- above the configured cap, and
        # above what charge-only widening (10.0) would ever see.
        charge_a = np.array([10.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        discharge_a = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        charge_b = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        discharge_b = np.array([20.0, 0.0, 0.0, 0.0, 0.0, 0.0])

        out = _widen(
            [
                (_cfg("ev1", "garage", 25.0), charge_a, discharge_a, 0.0, []),
                (_cfg("ev2", "garage", 25.0), charge_b, discharge_b, 0.0, []),
            ]
        )
        caps = [c.shared_charger_max_kw for c, _, _, _, _ in out]

        self.assertEqual(
            caps,
            [30.0, 30.0],
            "the widened cap must reach the real combined charge+discharge "
            "peak (30.0) the LP constraint actually models, not just the "
            "charge-only peak (10.0) -- current code widens to "
            f"{caps!r}, which leaves the oracle's own constraint tighter "
            "than what genuinely happened on this day.",
        )


if __name__ == "__main__":
    unittest.main()
