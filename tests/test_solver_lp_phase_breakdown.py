"""nimbus issue #773: one summary line per solve, covering every phase.

Measured on a real install across 16 consecutive cycles:
`phase2_secondary` took **30.1-56.8 s every single cycle** against a 60 s
per-call limit, with 80k-155k simplex iterations across 9-60
branch-and-bound nodes. Over that same window `phase1_primary` never
crossed the 5 s logging threshold at all.

That asymmetry is the lead, and the logging could not show it. Phase 1
solves the *same model* without the tie constraint, so it is the natural
control for what the tie constraint and the secondary objective actually
cost -- and precisely because it is fast, the 5 s gate discarded it. Only
the expensive phase was ever visible, which makes the cost look like a
property of the model rather than of one phase.

So `_timed_lp_call` now records every call, and `_phase_breakdown()`
emits a single line per solve **when some phase crossed the slow line**.
A healthy install stays exactly as silent as before.

What this file pins, in order of how easy each would be to break by
accident:

1. A fast solve logs nothing at all.
2. One slow phase brings the fast ones with it into the summary.
3. The level split follows the existing #773 discipline -- DEBUG for
   merely slow, WARNING for genuinely heading for trouble.
4. Recording never raises, and never leaks between solves on the same
   thread.
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

import _solver_path  # noqa: F401
from solver import lp


class _FakeHighs:
    """Only the two reads `_timed_lp_call` makes."""

    def __init__(self, iterations=1234, nodes=7, gap=0.0, status="Optimal"):
        self._info = MagicMock()
        self._info.simplex_iteration_count = iterations
        self._info.mip_node_count = nodes
        self._info.mip_gap = gap
        self._status = status

    def getInfo(self):
        return self._info

    def getModelStatus(self):
        return object()

    def modelStatusToString(self, _status):
        return self._status


class _Clock:
    """Deterministic elapsed times, one per `_timed_lp_call` entry/exit."""

    def __init__(self, durations):
        self._ticks = [0.0]
        for d in durations:
            self._ticks.append(self._ticks[-1] + d)
            self._ticks.append(self._ticks[-1])
        self._i = 0

    def __call__(self):
        v = self._ticks[min(self._i, len(self._ticks) - 1)]
        self._i += 1
        return v


class _Harness(unittest.TestCase):
    def _run_phases(self, durations, status="Optimal"):
        """Run N timed calls of the given durations inside one breakdown,
        returning the captured log records."""
        h = _FakeHighs(status=status)
        clock = _Clock(durations)
        original = lp.time.monotonic
        lp.time.monotonic = clock
        try:
            with self.assertLogs(lp._LOGGER, level=logging.DEBUG) as captured:
                # A DEBUG anchor so assertLogs never fails for lack of any
                # record -- the assertions below filter for the real line.
                lp._LOGGER.debug("anchor")
                with lp._phase_breakdown():
                    for i, _d in enumerate(durations):
                        with lp._timed_lp_call(h, f"lex_phase:p{i}"):
                            pass
        finally:
            lp.time.monotonic = original
        return captured.records

    @staticmethod
    def _breakdowns(records):
        return [r for r in records if "phase breakdown" in r.getMessage()]


class TestASolveWithNoSlowPhaseStaysSilent(_Harness):
    def test_nothing_is_emitted(self):
        """The whole point of the 5 s gate, preserved: a healthy install
        must not gain a new per-cycle log line from this change."""
        records = self._run_phases([0.1, 0.4, 0.2])
        self.assertEqual(self._breakdowns(records), [])


class TestOneSlowPhaseBringsTheFastOnesWithIt(_Harness):
    def setUp(self):
        # phase 1 fast, phase 2 slow -- the real measured shape.
        self.records = self._breakdowns(self._run_phases([0.6, 46.0]))

    def test_exactly_one_summary_line_per_solve(self):
        self.assertEqual(len(self.records), 1)

    def test_the_fast_phase_is_included(self):
        """Without this the summary would repeat what the old per-call
        line already said. The fast phase IS the finding."""
        msg = self.records[0].getMessage()
        self.assertIn("lex_phase:p0", msg)
        self.assertIn("0.6s", msg)

    def test_the_slow_phase_and_its_counters_are_included(self):
        msg = self.records[0].getMessage()
        self.assertIn("lex_phase:p1", msg)
        self.assertIn("46.0s", msg)
        self.assertIn("iters=1234", msg)
        self.assertIn("nodes=7", msg)

    def test_the_real_measured_shape_logs_at_warning(self):
        """46 s is past `_ALARMING_LP_CALL_SECONDS` -- half the 60 s
        per-call limit, i.e. 30 s -- so this warns.

        Named for what it asserts, after a first draft called it
        `..._merely_slow_solve_logs_at_debug` and then asserted WARNING.
        A test whose name contradicts its assertion is worse than no
        test: the name is what a reader greps for and believes without
        opening the body.

        This is also the real measured shape on the install (phase 1
        well under 5 s, phase 2 at 30-57 s), so in production this is
        the warning case, every cycle."""
        self.assertEqual(self.records[0].levelno, logging.WARNING)

    def test_a_slow_but_unalarming_solve_logs_at_debug(self):
        records = self._breakdowns(self._run_phases([0.6, 7.0]))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.DEBUG)


class TestANonOptimalPhaseIsAlwaysAlarming(_Harness):
    def test_status_not_optimal_forces_warning(self):
        """Matches the per-call rule this sits beside: a non-optimal
        result is an alarm regardless of how long it took."""
        records = self._breakdowns(self._run_phases([0.6, 7.0], status="Infeasible"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.WARNING)


class TestRecordingIsSafe(unittest.TestCase):
    def test_recording_outside_a_solve_is_a_no_op(self):
        """`_timed_lp_call` is also used by sweep_cost and the calibrate
        probes, which can run outside any breakdown. Those must not
        raise, and must not accumulate into whatever solve runs next.
        """
        lp._PHASE_LOG.entries = None
        lp._phase_record(("stray", 1.0, 1, 1, 0.0, "Optimal"))  # must not raise

    def test_entries_do_not_leak_between_solves(self):
        with lp._phase_breakdown():
            lp._phase_record(("a", 1.0, 1, 1, 0.0, "Optimal"))
            first = list(lp._PHASE_LOG.entries)
        with lp._phase_breakdown():
            self.assertEqual(lp._PHASE_LOG.entries, [])
        self.assertEqual(len(first), 1)

    def test_a_raising_phase_still_produces_the_breakdown(self):
        """The case most worth seeing the rest of the sequence for -- and
        the reason the wrapper sits outside _solve_with_options() rather
        than inside it.
        """
        h = _FakeHighs()
        clock = _Clock([0.5, 46.0])
        original = lp.time.monotonic
        lp.time.monotonic = clock
        try:
            with (
                self.assertLogs(lp._LOGGER, level=logging.DEBUG) as captured,
                self.assertRaises(RuntimeError),
                lp._phase_breakdown(),
            ):
                with lp._timed_lp_call(h, "lex_phase:p0"):
                    pass
                with lp._timed_lp_call(h, "lex_phase:p1"):
                    raise RuntimeError("phase blew up")
        finally:
            lp.time.monotonic = original
        msgs = [
            r.getMessage()
            for r in captured.records
            if "phase breakdown" in r.getMessage()
        ]
        self.assertEqual(len(msgs), 1)
        self.assertIn("lex_phase:p0", msgs[0])
        self.assertIn("lex_phase:p1", msgs[0])


if __name__ == "__main__":
    unittest.main()
