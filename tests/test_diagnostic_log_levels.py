"""Temporary per-issue diagnostic logging must not sit at WARNING on a
path that runs every solve cycle.

nimbus issue #757's own instrumentation is the cautionary case. Seven-ish
`Nimbus #757 diag:` messages were added at WARNING level and left running
on every cycle from v0.94.256 onward -- roughly forty releases. Measured
on a real install on 2026-09-13:

    a 2000-line error_log fetch spanned 31 minutes
    custom_components.nimbus_load  1095 lines  55%   (dominated by #757 diag)
    homeassistant.components.recorder 905 lines 45%
    -> ~122 occurrences of each diag message per 31 minutes

The concrete cost, hit first-hand: an investigation into **#757 itself**
was blocked, because the restart being investigated had already been
pushed out of the retrievable window by #757's own instrumentation. The
log is line-bounded, not time-bounded, so asking for a longer period does
not help.

DEBUG keeps every one of those messages available to anyone who raises
the log level while investigating, and stops them consuming the default
window for everyone else. That is what temporary per-issue diagnostics
should have been all along.

This test is scoped narrowly on purpose: it does NOT police log levels
generally. Plenty of genuine WARNINGs in this file should stay WARNINGs
(a dropped solar source, a capped activation, a failed publish). It
targets only the `#N diag:` convention -- messages explicitly marked as
temporary instrumentation for one issue -- which by construction fire on
a hot path and are meant to be read by someone actively debugging.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_SOLVER_WRITER = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)

# Matches the project's own temporary-instrumentation convention, e.g.
# "Nimbus #757 diag: ...".
_DIAG_RE = re.compile(r"#\d+ diag:")
_LOGGER_CALL_RE = re.compile(r"_LOGGER\.(debug|info|warning|error|critical)\s*\(")

# How far back to look for the _LOGGER call that owns a message line --
# the message is often the first argument on a following line.
_LOOKBACK = 4


def _diag_call_levels() -> list[tuple[int, str, str]]:
    """(line_number, level, message_snippet) for every `#N diag:` line
    that can be attributed to a _LOGGER call just above it."""
    lines = _SOLVER_WRITER.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        if not _DIAG_RE.search(line):
            continue
        for back in range(idx, max(-1, idx - _LOOKBACK), -1):
            m = _LOGGER_CALL_RE.search(lines[back])
            if m:
                found.append((idx + 1, m.group(1), line.strip()[:70]))
                break
    return found


class TestTemporaryDiagnosticsAreNotWarnings(unittest.TestCase):
    def test_no_per_issue_diag_message_logs_at_warning_or_above(self):
        noisy = [
            (ln, lvl, msg)
            for ln, lvl, msg in _diag_call_levels()
            if lvl in ("warning", "error", "critical")
        ]
        self.assertEqual(
            noisy,
            [],
            "temporary '#N diag:' instrumentation is logging at WARNING or "
            f"above on a per-solve path: {noisy}. That is how #757's own "
            "diagnostics grew to 55% of all log volume and blocked an "
            "investigation into #757 itself. Use _LOGGER.debug() -- the "
            "messages stay available to anyone raising the log level, "
            "without consuming the default window for everyone else.",
        )

    def test_the_scan_actually_finds_the_known_diag_messages(self):
        """Guards the guard. If the `#N diag:` convention were renamed, or
        the lookback stopped attributing messages to their _LOGGER call,
        the assertion above would pass while checking nothing."""
        found = _diag_call_levels()
        self.assertGreaterEqual(
            len(found),
            5,
            f"expected to find the known #757 diag call sites, found "
            f"{len(found)} -- the scan is no longer attributing diag "
            f"messages to their _LOGGER calls, so this file enforces "
            f"nothing.",
        )

    def test_every_found_diag_is_attributed_to_a_real_level(self):
        for ln, lvl, msg in _diag_call_levels():
            with self.subTest(line=ln, msg=msg):
                self.assertIn(lvl, ("debug", "info", "warning", "error", "critical"))


if __name__ == "__main__":
    unittest.main()
