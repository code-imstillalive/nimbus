"""IV&V finding (since-4812f93 pass, 2026-09-18): #1079's own pinning
test for the P2P bonus gate (`tests/
test_p2p_bonus_is_gated_to_committed_periods.py::
test_the_bonus_priced_oracle_is_gated_to_the_committed_periods`) cannot
see the majority real-world case it was written to guard.

`fetch_p2p_fixed_export_kw()` (`solver_writer.py`) returns, per period,
one of three values: a positive `rate_kw` inside a configured block, an
explicit `0.0` through the post-midnight self-consume closure only, or
`float("nan")` for every OTHER uncommitted period -- the ordinary case,
e.g. the ~41 kWh of midday solar export #1079's own issue names as the
second real harm this gate exists to prevent.

The existing end-to-end test asserts:

    self.assertTrue((bonus[committed <= 0.0] == 0.0).all())

`committed <= 0.0` is a numpy comparison, and any comparison against
NaN (other than `!=`) is always False -- so this mask silently excludes
every NaN-valued (ordinary, uncommitted) period and only ever checks
the small number of explicit-`0.0` (self-consume) periods. On the
reference household's real config this is roughly 52 NaN periods
against 16 explicit-zero ones in a single day.

Production code happens to be correct here today --
`p2p_bonus_price_by_period()`'s own `np.where(arr > 0.0, bonus_rate,
0.0)` also evaluates `NaN > 0.0` as False, so NaN periods correctly get
`0.0`. This test does not pin a live bug. It pins the gap in the
regression coverage: the exact scenario #1079's own docstring opens
with (a real settled P2P rate leaking into ordinary midday hours) is
the one case the existing end-to-end test's mask cannot see, on the
highest-severity of the three fixed call sites (the static-config
fallback that prices a live dispatch LP).

Reproduced during this pass: reintroducing the original defect in
`p2p_bonus_price_by_period()` (gating on `np.isnan(arr) | (arr > 0.0)`
instead of just `arr > 0.0` -- i.e. treating an uncommitted NaN period
as if it were committed) left every test in
`test_p2p_bonus_is_gated_to_committed_periods.py` passing. This test
constructs the same NaN-containing array directly and checks the
bonus at the NaN positions explicitly, so it would have caught that
exact regression.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
import solver_writer


class TestTheGateHandlesUncommittedNanPeriods(unittest.TestCase):
    def test_a_nan_uncommitted_period_gets_no_bonus(self):
        # Shape matching fetch_p2p_fixed_export_kw()'s own real output:
        # NaN for ordinary uncommitted hours, 0.0 only through the
        # post-midnight self-consume closure, a real rate inside the
        # committed block.
        fixed_export_kw = [float("nan"), float("nan"), 0.0, 12.0, 12.0, float("nan")]
        bonus_rate = 0.2183

        bonus = solver_writer.p2p_bonus_price_by_period(
            bonus_rate, fixed_export_kw, len(fixed_export_kw)
        )

        committed = np.asarray(fixed_export_kw, dtype=float)
        nan_mask = np.isnan(committed)

        self.assertTrue(
            nan_mask.any(), "the fixture must actually contain a NaN period"
        )
        self.assertTrue(
            (bonus[nan_mask] == 0.0).all(),
            "an ordinary uncommitted period (NaN in fetch_p2p_fixed_"
            "export_kw()'s own output -- not the explicit-0.0 self-"
            "consume closure) must not earn the settled bonus rate. "
            f"bonus at NaN periods: {bonus[nan_mask].tolist()}",
        )
        # The explicit-zero (self-consume) and real-committed periods
        # must still behave as the existing test already pins.
        self.assertEqual(bonus[2], 0.0)
        np.testing.assert_allclose(bonus[3:5], bonus_rate)

    def test_a_naive_gate_using_isnan_or_positive_would_fail_this(self):
        """Documents the exact regression this test would catch, so a
        future reader does not have to re-derive it. Not a test of
        production code -- a test of the mutation itself, to keep this
        file's own claim honest."""
        fixed_export_kw = [float("nan"), 0.0, 12.0]
        arr = np.asarray(fixed_export_kw, dtype=float)
        naive_regression = np.where(np.isnan(arr) | (arr > 0.0), 0.2183, 0.0)
        self.assertTrue(
            (naive_regression[np.isnan(arr)] != 0.0).all(),
            "sanity check: the naive isnan-or-positive gate this test "
            "guards against really does hand a bonus to a NaN period",
        )


if __name__ == "__main__":
    unittest.main()
