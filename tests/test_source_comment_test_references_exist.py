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
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION = _REPO_ROOT / "custom_components" / "nimbus_load"
_TESTS = _REPO_ROOT / "tests"

# A bare `test_foo.py`, or a `tests/test_foo.py` path form. Both appear
# in the tree today.
_REFERENCE = re.compile(r"\b(?:tests/)?(test_[A-Za-z0-9_]+\.py)\b")


def _existing_test_filenames() -> set[str]:
    return {p.name for p in _TESTS.rglob("test_*.py")}


def _references() -> dict[str, set[str]]:
    """{referenced filename: {source files naming it}} across the whole
    integration package."""
    found: dict[str, set[str]] = {}
    for path in sorted(_INTEGRATION.rglob("*.py")):
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
