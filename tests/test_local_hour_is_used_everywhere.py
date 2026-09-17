"""Every hour-of-day decision must be made in the HOUSEHOLD'S timezone.

`solver_writer.py` line 249 records what happens when one isn't:

    "the real 17:00-24:00 P2P window was actually being checked as
     UTC 17:00-24:00, which is AEST..."

That was fixed at the one site it was noticed at. The pattern that
allowed it -- reading `.hour` straight off a datetime and trusting
whatever tzinfo the caller attached -- was left in place at eighteen
others, including the network fee tiers, the scheduled discharge cost,
the salvage value, the self-consume block and the P2P window itself.

The failure is silent and it is expensive. A household's 17:00 P2P block
is 17:00 where they live. Gate it on a UTC hour in Brisbane and the
ten-hour offset lands the window on their solar CHARGING hours, where
export is genuinely zero -- so the scorer records the entire committed
block as undelivered, which then depresses EPR and inflates regret with
no error anywhere to say why.

The timezone is not a property this file controls: the same functions
are reached from the native integration, from the cron writer, and from
a `compute_quality_report` service call whose `start` a user types by
hand. So the rule is convert explicitly, never trust the input.

This test is an AST sweep rather than a grep, for the same reason
test_config_surface_budget.py is: a textual search cannot tell a real
`datetime.hour` read from `cov.hours` on a dataclass or from the word
appearing in a docstring, and a guard that quietly matches the wrong
thing is worse than none.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_SRC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)

# Attribute names that are NOT datetime fields. `hours` is a period-length
# array and a SeriesCoverage field; `hourly_*` are report dicts.
_NOT_A_CLOCK_READ = {"hours", "hourly_regret"}


def _is_local_call(node: ast.AST) -> bool:
    """True for `_local(x)` or `x.astimezone(...)` -- the two forms that
    have actually resolved the timezone before `.hour` is read."""
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "_local":
            return True
        if isinstance(fn, ast.Attribute):
            if fn.attr == "astimezone":
                return True
            # Walk through a .time()/.date() link so that the genuinely
            # correct `x.astimezone(LOCAL_TZ).time().hour` is not
            # reported. Found by this guard's own first run, which
            # flagged two already-correct sites -- a guard that cries
            # wolf gets switched off, so it has to see the real chain.
            if fn.attr in ("time", "date"):
                return _is_local_call(fn.value)
    return False


class TestEveryHourReadIsLocal(unittest.TestCase):
    def test_no_bare_dot_hour_or_dot_minute_read(self):
        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in ("hour", "minute"):
                continue
            if node.attr in _NOT_A_CLOCK_READ:
                continue
            if _is_local_call(node.value):
                continue
            # `foo.hours` etc. never reaches here -- attr is exactly
            # "hour"/"minute". What does reach here is a bare read off
            # something whose timezone was never resolved.
            offenders.append(f"line {node.lineno}: .{node.attr} read without _local()")
        self.assertEqual(
            offenders,
            [],
            "hour-of-day decisions must be made in the household's own "
            "timezone. Wrap the datetime in _local() before reading "
            ".hour/.minute -- it is an exact no-op when the value is "
            "already local, and it is what stops a UTC-stamped caller "
            "silently shifting the P2P window, the fee tiers or the "
            "salvage hour by the local UTC offset.\n\nOffenders:\n  "
            + "\n  ".join(offenders),
        )


class TestTheHelperItself(unittest.TestCase):
    def test_an_aware_datetime_is_converted(self):
        import sys

        sys.path.insert(0, str(_SRC.parent))
        from datetime import UTC, datetime

        import solver_writer

        # 14:00 UTC is midnight the next day in Brisbane (UTC+10).
        utc = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
        self.assertEqual(solver_writer._local(utc).hour, 0)

    def test_an_already_local_datetime_is_unchanged(self):
        import sys

        sys.path.insert(0, str(_SRC.parent))
        from datetime import datetime

        import solver_writer

        local = datetime(2026, 9, 16, 17, 30, tzinfo=solver_writer.LOCAL_TZ)
        self.assertEqual(solver_writer._local(local).hour, 17)
        self.assertEqual(solver_writer._local(local).minute, 30)

    def test_a_naive_datetime_is_returned_unchanged_not_guessed(self):
        """Converting a naive value would assume the machine's timezone --
        a second, different wrong answer. Unchanged matches the previous
        behaviour exactly, so this can never make a naive caller worse.
        """
        import sys

        sys.path.insert(0, str(_SRC.parent))
        from datetime import datetime

        import solver_writer

        # The suppression below is deliberate: a naive datetime is
        # exactly what this test exists to pin the handling of, so
        # passing a tzinfo would test nothing at all.
        naive = datetime(2026, 9, 16, 17, 0)  # noqa: DTZ001
        self.assertEqual(solver_writer._local(naive).hour, 17)
        self.assertIs(solver_writer._local(naive), naive)

    def test_the_p2p_window_gate_is_the_case_this_protects(self):
        """The concrete failure: a 17:00-24:00 local block, gated on a
        UTC-stamped datetime, lands on 03:00-10:00 local -- the solar
        charging hours, where export is genuinely zero."""
        import sys

        sys.path.insert(0, str(_SRC.parent))
        from datetime import UTC, datetime

        import solver_writer

        # Real 18:00 Brisbane, carried as UTC.
        evening = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
        self.assertFalse(17 <= evening.hour < 24, "bare read misses the block")
        self.assertTrue(17 <= solver_writer._local(evening).hour < 24)


if __name__ == "__main__":
    unittest.main()
