"""No message may claim Nimbus does not dispatch. It does.

## Why this is a test and not just a reworded string

Two places asserted that Nimbus does not act, both inside
`_log_dispatch_dry_run()`:

    "no command sent, live dispatch is not implemented yet."   (per-cycle log)
    "Nimbus has never written to an inverter"                  (docstring)

Both were true of **that function**, which sends no command and is not supposed
to. Both were false as statements about the install. On the reference household
Nimbus has driven real Modbus dispatch daily for weeks, through an HA automation
that reads the published plan — the write happens outside this integration
entirely, which is exactly why this code can be honestly observe-only while the
system it belongs to is not.

Nothing in either sentence said which of the two it meant, and the log line was
emitted **every cycle** next to a real kW figure. A reader — or an agent reading
a log — had every reason to conclude the plan was inert. **That misreading has
already produced a production false alarm**, and the household's own operating
notes now carry a standing rule against describing Nimbus as shadow-mode,
observe-only, dry-run or pending-a-trial. The old log line said three of those
four things once a minute.

It is also a claim that ages badly in silence. "not implemented **yet**" was
accurate when written. What changed was somewhere else — an automation — so no
change to this file was ever prompted, and nothing brought the sentence back for
review. A guard is the only thing that notices.

## What this checks, and what it deliberately does not

It checks the **claim**, not the wording: no user-visible string in
`solver_runtime.py` may assert that dispatch is unimplemented or that Nimbus has
never written anywhere. Rewording around the guard would need someone to make a
new claim of the same kind, which is the thing worth catching.

It does **not** forbid "dry-run" or "observe-only" as terms. They are accurate
names for this function and for the switch that gates it (`switch.nimbus_solver_
dispatch_dry_run`), and banning them would force a rename of a real entity to
satisfy a test. The distinction is between naming a code path and making a claim
about the system.
"""

from __future__ import annotations

import inspect
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime

_SRC = Path(solver_runtime.__file__).read_text(encoding="utf-8")

#: Claims about the SYSTEM that are false on a live install. Each is matched
#: case-insensitively against the whole module, including comments and
#: docstrings, because a comment is read by exactly the audience that gets
#: misled.
_FALSE_CLAIMS = (
    r"live dispatch is not implemented",
    r"dispatch is not implemented",
    r"Nimbus has never written",
    r"Nimbus never writes",
    r"0% operational control",
    r"pending a live trial",
)


def _uncommented_claim_sites(pattern: str) -> list[str]:
    """Every line matching `pattern`, minus the ones that are quoting the old
    wording in order to explain why it was wrong.

    Without this the fix's own explanatory comment would trip the guard, and
    the honest options would be to delete the explanation or to weaken the
    check. A line that cites the old text as history is marked, and marked
    lines are exempt.
    """
    out = []
    for line in _SRC.splitlines():
        if not re.search(pattern, line, re.IGNORECASE):
            continue
        # A historical citation, not a live claim.
        if "wording was" in line or "until nimbus issue" in line:
            continue
        out.append(line.strip())
    return out


class TestNoLiveClaimThatNimbusDoesNotDispatch(unittest.TestCase):
    def test_no_false_system_level_claim_survives(self):
        offenders: list[str] = []
        for pattern in _FALSE_CLAIMS:
            for line in _uncommented_claim_sites(pattern):
                offenders.append(f"{pattern!r}: {line[:110]}")
        self.assertEqual(
            offenders,
            [],
            "solver_runtime.py asserts that Nimbus does not dispatch. On the "
            "reference household it dispatches daily via an HA automation "
            "reading the published plan, so this is false about the install "
            "even when it is true about the function -- and it has already "
            "caused a production false alarm (nimbus #1333). Say which "
            "dispatch path is meant: " + " | ".join(offenders),
        )

    def test_the_log_line_names_the_path_it_is_talking_about(self):
        """The positive half. Removing the false claim is not enough — the line
        has to say whose dispatch it means, or a reader is left to guess again."""
        src = inspect.getsource(solver_runtime._log_dispatch_dry_run)
        self.assertIn("integration-internal", src)
        self.assertIn("dispatch automation", src)

    def test_it_still_says_this_function_sends_nothing(self):
        """The claim that IS true must survive. A reader of this log line needs
        to know no command went out from here; the fix narrows the scope, it
        does not remove the fact."""
        src = inspect.getsource(solver_runtime._log_dispatch_dry_run)
        self.assertIn("sends no command", src)


class TestTheGuardIsNotVacuous(unittest.TestCase):
    def test_the_patterns_would_match_the_original_wording(self):
        """If the regexes drifted so that none could ever match, the real test
        above would pass while checking nothing. Run them against the exact
        strings that used to be in the file."""
        original = [
            "live dispatch is not implemented yet.",
            "observe-only. Nimbus has never written to an inverter -- this",
        ]
        for text in original:
            with self.subTest(text=text[:40]):
                self.assertTrue(
                    any(re.search(p, text, re.IGNORECASE) for p in _FALSE_CLAIMS),
                    f"no pattern matches the original wording {text!r}",
                )

    def test_the_historical_exemption_is_narrow(self):
        """The exemption exists so the fix can explain itself. It must not be
        broad enough to let a fresh claim through — a new line asserting Nimbus
        does not dispatch would have to also contain "wording was" or "until
        nimbus issue" to be skipped, which is not something an ordinary message
        says."""
        fresh = '            "live dispatch is not implemented yet.",'
        self.assertTrue(any(re.search(p, fresh, re.IGNORECASE) for p in _FALSE_CLAIMS))
        self.assertNotIn("wording was", fresh)
        self.assertNotIn("until nimbus issue", fresh)


class TestAccurateTermsAreStillAllowed(unittest.TestCase):
    """Guard against over-correction. "dry-run" names a real switch."""

    def test_dry_run_is_not_banned(self):
        self.assertIn("dispatch_dry_run", _SRC)

    def test_observe_only_is_still_used_about_the_function(self):
        src = inspect.getsource(solver_runtime._log_dispatch_dry_run)
        self.assertIn("observe-only", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
