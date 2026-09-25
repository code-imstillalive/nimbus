"""IV&V finding (af5c3f8..620f39b pass, 2026-09-25, head issue #1207): the
Regret card's "Re-score with settlement" button (added in #1201/nimbus
issue #1200) advertises repairing one flagged day for one oracle MILP,
but the service it actually calls has no way to ask for that.

**The claim.** #1201's own CHANGELOG entry, about `only_dates` on
`rescore_quality_history()`:

    "only_dates on rescore_quality_history(), filtered before the oracle
    solve, so repairing one row costs one MILP instead of thirty."

**What the button actually does.** `nimbus-regret-card.js`'s click
handler calls:

    await this._hass.callService("nimbus_load", "rescore_history", {
      days: this._daysBack(dateKey),
    });

`_daysBack(dateKey)` is the calendar-day distance from today back to the
flagged day, clamped to [1, 30] -- exactly the `rescore_history` service's
own `days` range. The service (`services.py`) has no `date`/`only_dates`
parameter at all; `_async_handle_rescore_history()` calls
`solver_writer.rescore_quality_history(cfg, dt_util.now(), days)` with no
`only_dates`, which -- by design, per its own docstring -- scores every
day from today back to `days` ago, not just the flagged one.

So the `only_dates` optimization this commit built (and used correctly
in the *automatic* sweep, `repair_provisional_quality_history()`, capped
at one repair per cycle) never reaches the button. Clicking it on a row
5 days old costs 5 MILPs; at the button's own 30-day clamp, 30 -- in one
synchronous `hass.async_add_executor_job()` call. This is exactly the
#773/#757 executor-starvation shape #1201's own docstrings cite as the
reason the automatic sweep is capped, re-entering through the manual
button instead of a repeating loop.

This test reproduces the button's own call shape exactly, through the
real `rescore_quality_history()` entry point (no mocking of intent, only
of the real recorder/oracle calls each day would otherwise need) --
mirroring `tests/test_1200_provisional_row_repair.py`'s own
`TestOnlyDatesNarrowsWithoutPayingForTheRest` pattern -- and asserts the
one property the button's own design (and #1201's CHANGELOG) claims:
repairing one flagged day costs one MILP, however many days old it is.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

import _solver_path  # noqa: F401
import pytest
import solver_writer


def _entry(status="applied"):
    return {
        "epr": 0.9,
        "j_ref": 5.0,
        "j_ach": -20.0,
        "j_star": -21.0,
        "regret_dollars": 1.0,
        "real_p2p_settlement_status": status,
    }


def _now(day=25):
    return datetime(2026, 9, day, 10, 0, tzinfo=solver_writer.LOCAL_TZ)


class TestTheButtonsOwnCallShape(unittest.TestCase):
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "nimbus #1208: nimbus_load.rescore_history has no way to target "
            "a single day, so nimbus-regret-card.js's own days-back call "
            "shape (which is all the service accepts) pays for every "
            "intervening day, not just the flagged one."
        ),
    )
    def test_repairing_one_flagged_day_five_days_back_costs_one_milp(self):
        computed: list[str] = []

        def compute(cfg, day_start, day_end, allow_partial):
            computed.append(day_start.date().isoformat())
            return _entry("applied")

        with (
            mock.patch.object(
                solver_writer, "_compute_report_for_window", side_effect=compute
            ),
            mock.patch.object(
                solver_writer,
                "ha_get",
                return_value={"attributes": {}, "state": "90"},
            ),
            mock.patch.object(solver_writer, "ha_post_state"),
        ):
            # Reproduces nimbus-regret-card.js's _daysBack() + callService
            # shape exactly: the ONLY way the button can reach a day 5
            # days back is `days=5`, since the service has no per-date
            # parameter to narrow with.
            solver_writer.rescore_quality_history({}, _now(day=25), 5)

        self.assertEqual(
            len(computed),
            1,
            f"repairing the one flagged day cost {len(computed)} oracle MILP "
            f"solve(s) ({computed}), not the 1 the button's own design and "
            "#1201's CHANGELOG both claim",
        )


if __name__ == "__main__":
    unittest.main()
