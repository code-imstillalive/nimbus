"""nimbus issue #773: the diagnostic that separates the two remaining
explanations for `phase2_pin_resolve` coming back `Infeasible`.

That phase is supposed to be a trivially feasible re-solve.
`_pin_binaries_to_current_solution()` pins every binary to the value the
previous solve just returned, so that solution stays feasible by
construction — its own comment says the re-solve "reproduces the
identical solution; the point is a clean solve record, not a different
answer."

An `Infeasible` result contradicts that outright, and it has been
observed: twice in the logs that prompted this, and — per `lp.py`'s own
cooldown comment — **14 times in under 8 minutes** on an earlier
occasion, badly enough to starve the executor and break an unrelated
component's recorder lock.

Two candidates remain:

1. **The rounding is unconditional.** A value that is not actually
   near-integral gets snapped anyway, and against a tight semi-continuous
   linking constraint (#616's `power[t] <= max_power * on[t]`) a snapped
   assignment can genuinely be infeasible.
2. **`h.val()` after a MIP solve may not return the incumbent**, in which
   case the values being rounded were never an integer solution at all.

One `max()` separates them: if the worst binary is materially off
integral, it is (1); if they are all crisp, it is (2). That is a much
cheaper discriminator than the captured model file or scaled synthetic
repro every earlier lead on #773 needed.

These tests pin the diagnostic's own behaviour — that it fires when it
should, stays quiet when it should, and above all **does not change the
pinning**. A diagnostic that perturbs the thing it measures would be
worse than none.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from solver import lp


class _FakeHighs:
    """Only the three calls `_pin_binaries_to_current_solution()` makes."""

    def __init__(self, values):
        self._values = values
        self.bounds_set: list[tuple[int, float, float]] = []
        self.made_continuous: list[int] = []

    def val(self, handle):
        return self._values[handle]

    def changeColIntegrality(self, i, _kind):
        self.made_continuous.append(i)

    def changeColBounds(self, i, lo, hi):
        self.bounds_set.append((i, lo, hi))


def _pin(values, binary_cols):
    h = _FakeHighs(values)
    var_array = list(range(len(values)))
    lp._pin_binaries_to_current_solution(h, var_array, binary_cols)
    return h


class TestItStaysSilentOnACleanSolve(unittest.TestCase):
    def test_crisply_integral_values_log_nothing(self):
        """The healthy case, and by far the common one. A diagnostic that
        fires every cycle is one nobody reads."""
        with self.assertNoLogs(lp._LOGGER, level="WARNING"):
            _pin([0.0, 1.0, 0.0, 1.0], [0, 1, 2, 3])

    def test_values_inside_the_solver_tolerance_log_nothing(self):
        """HiGHS satisfies integrality to a tolerance, not exactly. A
        binary at 1 - 1e-9 is a clean integer assignment and must not be
        reported as an anomaly."""
        with self.assertNoLogs(lp._LOGGER, level="WARNING"):
            _pin([1e-9, 1.0 - 1e-9], [0, 1])


class TestItFiresOnAMateriallyOffIntegralBinary(unittest.TestCase):
    def test_a_half_way_value_is_reported(self):
        """The shape that would confirm candidate (1): a value that was
        never a clean integer assignment, about to be snapped anyway."""
        with self.assertLogs(lp._LOGGER, level="WARNING") as captured:
            _pin([0.0, 0.5, 1.0], [0, 1, 2])
        joined = "\n".join(captured.output)
        self.assertIn("#773", joined)
        self.assertIn("away from integral", joined)

    def test_the_message_names_the_worst_gap_and_the_count(self):
        """Whoever reads this in a log needs the magnitude, not just the
        fact — it is the number that decides between the two candidates."""
        with self.assertLogs(lp._LOGGER, level="WARNING") as captured:
            _pin([0.0, 0.4, 1.0, 1.0], [0, 1, 2, 3])
        joined = "\n".join(captured.output)
        self.assertIn("4 binaries", joined)
        self.assertIn("4.000e-01", joined)


class TestTheDiagnosticDoesNotChangeThePinning(unittest.TestCase):
    """The property that matters most. This sits on the real solve path;
    measuring must not perturb."""

    def test_every_binary_is_still_pinned_to_its_rounded_value(self):
        """Note the third value: Python's `round()` is banker's rounding,
        so an exactly-0.5 binary pins to **0**, not 1. Surprising, but it
        is the pre-existing behaviour and this diagnostic must not change
        it — recorded here so the next reader does not mistake it for a
        typo in the expectation."""
        h = _pin([0.0, 0.9999999, 0.5, 1.0], [0, 1, 2, 3])
        self.assertEqual(
            h.bounds_set,
            [(0, 0.0, 0.0), (1, 1.0, 1.0), (2, 0.0, 0.0), (3, 1.0, 1.0)],
            "the diagnostic altered how binaries are pinned -- it is "
            "measurement only and must leave this identical",
        )

    def test_every_binary_is_still_relaxed_to_continuous(self):
        h = _pin([0.0, 0.5, 1.0], [0, 1, 2])
        self.assertEqual(h.made_continuous, [0, 1, 2])

    def test_non_binary_columns_are_left_alone(self):
        """`binary_cols` is a subset; the continuous variables around it
        must not be touched."""
        h = _pin([0.3, 1.0, 7.25], [1])
        self.assertEqual(h.bounds_set, [(1, 1.0, 1.0)])
        self.assertEqual(h.made_continuous, [1])

    def test_an_empty_binary_list_is_a_no_op(self):
        """A pure LP reaches this with nothing to pin."""
        h = _pin([0.3, 7.25], [])
        self.assertEqual(h.bounds_set, [])
        self.assertEqual(h.made_continuous, [])


if __name__ == "__main__":
    unittest.main()
