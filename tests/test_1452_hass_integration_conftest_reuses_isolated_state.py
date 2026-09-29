"""IV&V finding (7ad927b..e497a7c pass, nimbus issue #1452): `tests/hass_
integration/conftest.py`'s own `solver_state_in_tmp_path` fixture carried a
second, hand-duplicated copy of `_isolated_state.PERSISTED_PATH_CONSTANTS`'
name list, and one entry drifted: it spelled `LOAD_ERROR_NOTIFIED_PATH`
where the real `solver_writer` constant is `LOAD_FORECAST_ERROR_NOTIFIED_
PATH`. The fixture's own `if hasattr(solver_writer, name):` guard made the
mismatch silent -- that one persisted path was never redirected into
`tmp_path` for any `tests/hass_integration/` run, so a real solve inside
that suite could read or write the real `/opt/nimbus_solver_load_forecast_
error.txt` production path, the exact failure class nimbus issue #1330
exists to prevent.

Fixed by having the fixture delegate to `_isolated_state.isolated_state_
paths()` -- the same helper `test_1330_main_driving_tests_isolate_state_
paths.py` already guards -- instead of maintaining its own list, which
makes a second drift structurally impossible rather than merely unlikely.

`tests/hass_integration/` itself needs the real `pytest-homeassistant-
custom-component` harness, which this sandbox cannot build (see the
SessionStart hook's own note), so this cannot be pinned by actually running
that suite. Pinned instead as a source-level guard: the fixture's own
source must delegate to the shared helper and must not define a second
name/filename mapping of its own.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_CONFTEST_PATH = Path(__file__).resolve().parent / "hass_integration" / "conftest.py"
_SRC = _CONFTEST_PATH.read_text(encoding="utf-8")


class TestTheFixtureDelegatesRatherThanDuplicates(unittest.TestCase):
    def test_the_fixture_imports_the_shared_helper(self):
        self.assertIn(
            "from _isolated_state import isolated_state_paths",
            _SRC,
            "solver_state_in_tmp_path no longer imports the shared "
            "_isolated_state.isolated_state_paths() helper -- if it went "
            "back to a hand-copied name list, nimbus #1452's own drift "
            "class (a name silently wrong, guarded by hasattr) can recur.",
        )

    def test_the_fixture_calls_the_shared_helper(self):
        self.assertIn(
            "isolated_state_paths(solver_writer)",
            _SRC,
            "solver_state_in_tmp_path must actually call "
            "isolated_state_paths(solver_writer), not just import it.",
        )

    def test_no_hand_written_persisted_path_name_survives(self):
        """The exact drifted literal from nimbus #1452, and its sibling
        real names, must not appear as hand-typed string literals in this
        file any more -- if they do, someone re-introduced a second,
        independently-maintained copy of the name list."""
        tree = ast.parse(_SRC, filename=str(_CONFTEST_PATH))
        literal_strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # The drifted name itself must never reappear, in any form.
        self.assertNotIn(
            "LOAD_ERROR_NOTIFIED_PATH",
            literal_strings,
            "the drifted name from nimbus #1452 is back as a string "
            "literal in conftest.py",
        )
        # None of the four real persisted-path constant names should be
        # hand-typed here either -- they belong in _isolated_state.py alone.
        for real_name in (
            "PLAN_STATE_PATH",
            "SOLAR_DELIVERY_RATIO_PATH",
            "LOCK_PATH",
            "LOAD_FORECAST_ERROR_NOTIFIED_PATH",
        ):
            with self.subTest(name=real_name):
                self.assertNotIn(
                    real_name,
                    literal_strings,
                    f"{real_name!r} is hand-typed as a string literal in "
                    "conftest.py again -- this fixture should only "
                    "reference the shared _isolated_state list, never "
                    "spell a persisted-path name itself.",
                )


if __name__ == "__main__":
    unittest.main()
