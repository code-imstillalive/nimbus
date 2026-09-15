"""Every `test_*.py` filename a source comment points at must exist.

Found 2026-09-15 while looking for the generic `_unrecorded_attributes`
guard for the flagship push sensors: `sensor.py` said

    See test_sensor_commanded_state_unrecorded_attributes.py's own
    generic guard -- it asserts every list-valued field LoadRunState.
    to_dict() can produce is in this frozenset

and no such file has ever existed. The guard it describes is real and
does exactly that -- it just lives in
`test_sensor_forecast_unrecorded_attributes.py`. An audit of the whole
integration then turned up a second one: `solver/elements.py` credited
the exactly-100%-efficiency rejection to `test_network_synthetic.py`,
which is also gone (the check lives in
`test_elements_battery_config_validation.py`).

Neither is cosmetic. A comment naming a test file is load-bearing
documentation -- it is how the next person answers "is this constraint
actually enforced, or just asserted in prose?" A name that resolves to
nothing reads as *no* rather than *elsewhere*, and the natural response
to "no" is to write a second, duplicate guard, or to assume the
invariant is unguarded and stop trusting it. That is the same shape as
`solver/README.md`'s own "not wired into anything" claim (nimbus issue
#364), which went unnoticed for months and is what
`test_readme_entity_references_exist.py` next door exists to prevent --
this file is that idea pointed at the source tree instead of the docs.

Scoped to `test_*.py` specifically because that is a name this repo can
resolve with certainty: it either is a file under `tests/` or it is not.
Deliberately not extended to prose references ("see the solver's own
docstring"), which have no checkable referent.

WHAT A GREEN RUN OF THIS FILE DOES NOT MEAN (nimbus issue #955, Mark
Purcell, 48-hour IV&V #950)
--------------------------------------------------------------------
**This checks that a referenced file EXISTS. It does not check that the
file contains the guard the comment claims lives there.** A comment
saying *"see `test_foo.py`'s own guard for X"* passes as long as
`test_foo.py` exists at all -- even if that file has since been
rewritten to test something else entirely. The two real regressions this
file was built for are still caught, because both filenames genuinely
resolved to nothing; the subtler case -- name survives, content diverges
-- is out of scope, and a reader should not over-trust a green run here.

Two content heuristics were measured against the real corpus before
settling for saying so plainly, because "document the limitation" is
otherwise too easy an answer:

1. **Bidirectional issue link** -- require the issue number cited near
   the reference to also appear in the referenced test file. 11 of 22
   references carry an issue number nearby; only 6 of those 11 would
   pass. The 5 failures are legitimate (the issue number next to a
   reference is frequently the issue being *discussed*, not the one the
   test was written under).
2. **Enclosing symbol** -- require the def/class containing the comment
   to be named somewhere in the referenced test file. 7 of 12 resolvable
   references pass, 5 fail legitimately (a test can guard
   `_build_plan_once`'s behaviour perfectly well while only ever calling
   the public `build_plan`), and 10 of the 22 sit in module-level
   comments with no enclosing symbol to check at all.

Both would fail on roughly 40% of a corpus with no real defects in it,
and a guard that cries wolf twice a week gets an allowlist bolted on and
then gets ignored. "Does this file test X" is a genuinely harder
question than "does this file exist", and the honest answer was to keep
the check precise and state its ceiling here.

What WAS extended in response to #955 is coverage rather than depth: the
sweep now also reads the standalone/cron writer under
`docs/real-world-integration/files/`. That is real shipped source with 5
references of its own and no guard on any of them -- the same class of
stale name, in a file a household actually runs, going entirely
unchecked. It is clean today, so this lands green.

`tests/` itself is deliberately still excluded. It carries 97
references, of which the only unresolvable ones are this file's own
illustrative examples (`test_foo.py`, and the two historical names
quoted above) plus a `test_X.py` placeholder in `_solver_path.py` --
so guarding it would mean an allowlist of deliberate non-names, for
references that are far less load-bearing than a source comment.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION = _REPO_ROOT / "custom_components" / "nimbus_load"
_TESTS = _REPO_ROOT / "tests"
# nimbus issue #955: the standalone/cron writer is real shipped source a
# household actually runs, and its own test-file references were never
# swept. Same guarantee, same failure shape, one more root.
_STANDALONE = _REPO_ROOT / "docs" / "real-world-integration" / "files"
_SCANNED_ROOTS = (_INTEGRATION, _STANDALONE)

# A bare `test_foo.py`, or a `tests/test_foo.py` path form. Both appear
# in the tree today.
_REFERENCE = re.compile(r"\b(?:tests/)?(test_[A-Za-z0-9_]+\.py)\b")


def _existing_test_filenames() -> set[str]:
    return {p.name for p in _TESTS.rglob("test_*.py")}


def _references() -> dict[str, set[str]]:
    """{referenced filename: {source files naming it}} across the whole
    integration package, plus the standalone/cron writer (#955)."""
    found: dict[str, set[str]] = {}
    for root in _SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in _REFERENCE.finditer(text):
                rel = path.relative_to(_REPO_ROOT).as_posix()
                found.setdefault(match.group(1), set()).add(rel)
    return found


class TestSourceCommentsPointAtRealTestFiles(unittest.TestCase):
    def test_every_referenced_test_file_exists(self):
        existing = _existing_test_filenames()
        references = _references()
        self.assertTrue(
            references,
            "sanity check: found no test-file references in the integration "
            "at all, so this test would pass against any source tree",
        )
        broken = {
            name: sorted(where)
            for name, where in sorted(references.items())
            if name not in existing
        }
        self.assertEqual(
            broken,
            {},
            "these source comments name a test file that does not exist. A "
            "name that resolves to nothing reads as 'this invariant is "
            "unguarded' -- point it at the file that really carries the "
            "guard (it usually still exists under a different name after a "
            "split or rename), or drop the reference if the guard is "
            f"genuinely gone: {broken}",
        )

    def test_the_standalone_writer_is_actually_being_swept(self):
        """Guard on the mechanism, not the data (nimbus issue #955).

        Adding `_STANDALONE` to the scanned roots is worth nothing if a
        later refactor moves or renames that directory: `rglob` on a
        missing path yields nothing and raises nothing, so this file
        would keep passing while silently dropping back to
        integration-only coverage -- the exact "enforcing less, still
        green" shape #955 is about.
        """
        self.assertTrue(
            _STANDALONE.is_dir(),
            f"{_STANDALONE} no longer exists -- the standalone/cron writer has "
            "moved, and this file is silently no longer sweeping it",
        )
        sources = {where for wheres in _references().values() for where in wheres}
        self.assertTrue(
            any(s.startswith("docs/real-world-integration/files/") for s in sources),
            "no test-file reference was found in the standalone/cron writer at "
            "all. Either every one was removed (fine -- delete this test with "
            "them), or the sweep has stopped reaching that directory.",
        )

    def test_the_two_known_corrections_stay_correct(self):
        """The specific pair this file was written for, pinned by name so
        a future edit that reverts either one fails with the reason
        attached rather than as an anonymous entry in the sweep above."""
        existing = _existing_test_filenames()
        for expected in (
            "test_sensor_forecast_unrecorded_attributes.py",
            "test_elements_battery_config_validation.py",
        ):
            with self.subTest(expected):
                self.assertIn(expected, existing)


if __name__ == "__main__":
    unittest.main()
