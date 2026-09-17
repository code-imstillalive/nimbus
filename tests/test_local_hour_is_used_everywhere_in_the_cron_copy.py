"""IV&V finding (since-4812f93 pass, 2026-09-18): #1076's AST guard
(`tests/test_local_hour_is_used_everywhere.py`) protects only
`custom_components/nimbus_load/solver_writer.py` -- the standalone/cron
deployment's own copy, `docs/real-world-integration/files/
nimbus_solver_forecast_writer.py`, has no equivalent guard at all.

That copy genuinely HAS its own `_local()` (ported in the same #1076
commit, line ~193) and uses it at most of the sites that matter
(the P2P window, the 5-minute AEMO bucket index, the midnight-rollover
scan). But a plain AST sweep of the SAME shape as the native guard,
run against this file, finds five remaining bare `.hour`/`.minute`
reads the port missed:

    line 3702  now.minute            (seconds_to_settlement_capture)
    line 4807  grid_times[0].hour / .minute   (AEMO P5MIN bucket index)
    line 4955  t.hour                (discharge_cost_arr comprehension)
    line 4957  grid_times[-1].hour   (salvage_value)

Three of the four (`grid_times[...]`) are currently harmless in
practice, but only because this file's own `main()` happens to always
construct `now` as `datetime.now(UTC).astimezone(BRISBANE_TZ)` before
calling `build_tiered_grid(now)` -- every element of `grid_times` is
therefore already `BRISBANE_TZ`-aware by convention, not by anything
that STOPS it being something else. That is the exact fragile
assumption #1076's own module docstring names as the thing that failed
before: "'already local by convention' is precisely the assumption
that failed on the scoring side."

The fourth, `seconds_to_settlement_capture(now)` at line 3702, is a
real, live, latent bug for any deployment of this same cron script
outside a whole-hour UTC offset region -- e.g. Adelaide, UTC+9:30, a
real NEM region #1076's own commit message names specifically for
exactly this failure mode ("Brisbane is UTC+10 so minute-of-hour
happens to be invariant there, but Adelaide is UTC+9:30 -- a real NEM
region where an unconverted minute is also wrong"). The function is
documented as "a pure function of `now`" precisely so any caller can
feed it whatever `now` it has -- which means it cannot rely on its
current caller happening to pass a Brisbane-local value forever, the
same way the native integration's own fix could not rely on that either.

This test pins the AST-sweep gap itself (a copy of the native guard's
own walker, run against the cron file) rather than the specific defect,
since a future edit to either file could add or remove offenders
independently of this one pass's own findings. It fails against current
code because the cron file has 5 offenders and the native file (already
covered by its own, separate guard) has zero.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import pytest

_SRC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "docs"
    / "real-world-integration"
    / "files"
    / "nimbus_solver_forecast_writer.py"
)

# Same exemption list as the native guard's own test file -- neither
# name is a datetime field in this file either.
_NOT_A_CLOCK_READ = {"hours", "hourly_regret"}


def _is_local_call(node: ast.AST) -> bool:
    """Same recognizer as tests/test_local_hour_is_used_everywhere.py's
    own `_is_local_call()` -- `_local(x)` or `x.astimezone(...)`,
    walking through a `.time()`/`.date()` link."""
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "_local":
            return True
        if isinstance(fn, ast.Attribute):
            if fn.attr == "astimezone":
                return True
            if fn.attr in ("time", "date"):
                return _is_local_call(fn.value)
    return False


class TestTheCronCopyIsGuardedTheSameWay(unittest.TestCase):
    @pytest.mark.xfail(
        reason=(
            "nimbus IV&V (since 4812f93, 2026-09-18): the #1076 AST guard "
            "only parses custom_components/nimbus_load/solver_writer.py -- "
            "the standalone/cron copy has its own _local() but 5 bare "
            ".hour/.minute reads the port missed, one of them (line 3702, "
            "seconds_to_settlement_capture's now.minute % 5) a real latent "
            "bug for any non-whole-hour-UTC-offset NEM region (e.g. "
            "Adelaide, UTC+9:30). See the module docstring above for the "
            "full site list and reasoning."
        ),
        strict=True,
    )
    def test_no_bare_dot_hour_or_dot_minute_read_in_the_cron_copy(self):
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
            offenders.append(f"line {node.lineno}: .{node.attr} read without _local()")
        self.assertEqual(
            offenders,
            [],
            "the cron/standalone copy must be swept for bare .hour/.minute "
            "reads the same way the native integration already is -- a "
            "future edit to either copy can silently reintroduce exactly "
            "the #1076 bug in whichever copy has no guard.\n\nOffenders:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
