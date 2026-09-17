"""IV&V finding (since-#1058 pass, 2026-09-17): #1012's per-battery
energy balance (v0.94.369-373, `battery_energy_balance()`) reports
`implied_charge_efficiency`/`implied_discharge_efficiency` with full,
unflagged confidence on a genuinely MIXED window (real charge AND real
discharge throughput in the same window) -- exactly the shape a normal
calendar day produces, and exactly the shape
`achieved_energy_balance_by_battery` publishes on every real daily
quality report.

The function's own docstring is explicit that the single-direction
implied value is only DECISIVE when the other direction is held at its
configured value deliberately because it has no (or negligible)
throughput of its own to disturb that assumption -- e926a10's own commit
message spells out why a pure single-direction window is required: a
charge-only window yields the degenerate product `e*k`, and mixing both
directions inside one net swing is precisely the case the SEPARATE
"joint solve deliberately NOT done here" reasoning exists to avoid,
because "measured" is a NET swing that cannot be attributed to either
side.

But the code does not apply that reasoning symmetrically. It computes
`implied_charge_efficiency` whenever `in_kwh > 1e-6` -- with no check
that `out_kwh` is negligible -- using the CONFIGURED discharge
efficiency to net out the discharge side. If the true discharge
efficiency differs from configured (which is the exact unknown #1012
exists to measure), the resulting "implied charge efficiency" is biased
by that wrong assumption, and the reverse is equally true for
`implied_discharge_efficiency`. `implied_efficiency_reason` stays `None`
in this case -- the same value a genuinely decisive pure-direction
window gets -- so nothing in the returned dict tells a reader "this
number assumes the OTHER direction's configured value is correct,
which is the very thing being investigated."

This test constructs a window with real charge AND discharge throughput
where the TRUE efficiencies (0.90 charge, 0.80 discharge) both differ
from the CONFIGURED ones (0.95/0.95) -- a realistic, not adversarial,
scenario for exactly the household #1012 is about, where the configured
numbers are suspected wrong in both directions at once. It asserts the
function should flag reduced confidence (a non-None
`implied_efficiency_reason`) on a window where both directions carry
real throughput, since neither implied value can be decisive there. It
fails against current code because no such flag exists -- both implied
values are returned as filled-in numbers with `reason=None`.

Fix shape suggested (not prescribed): only report the single-direction
implied efficiency as decisive when the OTHER direction's throughput is
itself negligible (mirroring the existing 1e-6 throughput floor used
elsewhere in this same function); on a genuinely mixed window, either
return the implied values with a distinguishing reason (e.g.
"mixed_window_not_decisive") or leave them as diagnostic-only fields the
caller is told not to trust as a standalone capacity/efficiency
estimate the way 0d2fd70's own changelog table did.
"""

from __future__ import annotations

import unittest

import pytest

import _solver_path  # noqa: F401
import solver_writer


def _bal(**over):
    kw = {
        "name": "home",
        "in_kwh": 100.0,
        "out_kwh": 50.0,
        "initial_soc_kwh": 10.0,
        "final_soc_kwh": 10.0,
        "capacity_kwh": 120.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
    }
    kw.update(over)
    return solver_writer.battery_energy_balance(**kw)


class TestAMixedWindowFlagsReducedConfidence(unittest.TestCase):
    @pytest.mark.xfail(
        reason=(
            "nimbus IV&V (since #1058, 2026-09-17): battery_energy_balance() "
            "computes implied_charge_efficiency/implied_discharge_efficiency "
            "with reason=None on a genuinely mixed (charge+discharge) window, "
            "silently assuming the OTHER direction's configured efficiency is "
            "correct -- which is exactly the unknown #1012 exists to measure. "
            "See the module docstring above for the full reasoning."
        ),
        strict=True,
    )
    def test_mixed_direction_window_does_not_read_as_fully_decisive(self):
        in_kwh, out_kwh = 100.0, 50.0
        # TRUE one-way efficiencies, both genuinely different from the
        # 0.95/0.95 CONFIGURED values below -- the exact "both wrong, in
        # different directions" shape #1012's own real measurements
        # found on the reference household (0d2fd70's changelog table).
        true_charge_eff, true_discharge_eff = 0.90, 0.80
        measured = in_kwh * true_charge_eff - out_kwh / true_discharge_eff

        r = _bal(
            in_kwh=in_kwh,
            out_kwh=out_kwh,
            initial_soc_kwh=0.0,
            final_soc_kwh=measured,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
        )

        # Sanity: this is a real mixed window, not one where one side
        # happens to be negligible.
        self.assertGreater(in_kwh, 1e-6)
        self.assertGreater(out_kwh, 1e-6)

        self.assertIsNotNone(
            r["implied_efficiency_reason"],
            "a window with real throughput in BOTH directions, where the "
            "true efficiencies differ from configured in both directions "
            f"at once, was reported as fully decisive "
            f"(implied_charge_efficiency={r['implied_charge_efficiency']!r}, "
            f"implied_discharge_efficiency={r['implied_discharge_efficiency']!r}, "
            "reason=None) -- the same confidence level as a genuinely "
            "decisive pure-direction window, even though neither implied "
            "value here is decisive (each assumes the other direction's "
            "configured efficiency is correct, which is precisely what "
            "#1012 is trying to determine).",
        )


if __name__ == "__main__":
    unittest.main()
