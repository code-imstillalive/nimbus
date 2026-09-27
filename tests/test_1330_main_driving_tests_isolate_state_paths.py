"""A test that drives the real `main()` must not write production state.

## The defect this pins (nimbus issue #1330)

`solver_writer`'s four persisted paths each default to a real production file:

    PLAN_STATE_PATH                    /opt/nimbus_solver_last_plan.json
    SOLAR_DELIVERY_RATIO_PATH          /opt/nimbus_solver_solar_delivery_ratio.json
    LOCK_PATH                          /opt/nimbus_solver_forecast_writer.lock
    LOAD_FORECAST_ERROR_NOTIFIED_PATH  /opt/nimbus_solver_load_forecast_error.txt

Measured when this test was written: **nine test files call
`solver_writer.main()`. One isolated both data paths, five isolated only
`PLAN_STATE_PATH`, and three isolated none of them.** `/opt` is absent on CI and
on a Windows dev box, so every access failed, the failure was caught, and
nothing was visible — the property that let this survive. Run the suite on
either NUC and those tests read the live last-plan and solar-delivery-ratio
files into their assertions and then overwrite them.

Mark Purcell found the first symptom while building the golden-master harness
(#1328): `test_main_golden_output_guardrail.py` pointed `PLAN_STATE_PATH` at a
fixed `/tmp/nonexistent_plan_state_golden_test.json`, and `main()` **writes**
that file, so run two on one machine read run one's plan back through the
proximal term and `forecast[1]['shadow_price']` moved 0.2982 -> 0.2999.

## Why a guard test rather than only the fix

The `/tmp/nonexistent_*.json` pattern was a **convention**, repeated across six
files. Fixing the instances leaves the convention, and the next `main()`-driving
test written from a copy of an existing one inherits it. What follows checks the
property directly, so a new test either isolates its paths or fails here with
the reason.

## What it deliberately does NOT check

That `main()` itself uses good defaults. `/opt/...` is correct for the
standalone/cron deployment those defaults exist for, and the native integration
overrides all four (see `solver_runtime.set_default_env_vars`, and #1324 for why
two of them moved out of the config dir). The defect is a test suite writing
there, not the paths.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolated_state import PERSISTED_PATH_CONSTANTS

_TESTS = Path(__file__).resolve().parent

#: Files exempt from the isolation requirement, each with its reason. A test
#: whose whole subject IS one of these paths has to name it explicitly; adding
#: to this list is a deliberate act, which is the point of it being a list.
_EXEMPT = {
    # Asserts on the constants themselves via the AST; never runs main().
    "test_1324_deleted_runtime_files_are_not_in_config.py",
    # This file.
    "test_1330_main_driving_tests_isolate_state_paths.py",
}


def _test_files() -> list[Path]:
    return sorted(p for p in _TESTS.glob("test_*.py") if p.name not in _EXEMPT)


def _isolates(tree: ast.AST) -> bool:
    """`isolated_state_paths(...)` actually CALLED, not merely imported.

    Checking for the name as text passes on the import line alone. Proven by
    reintroducing the defect in one file: with the helper call swapped back for
    a hardcoded `patch.object`, a text check still passed because
    `from _isolated_state import isolated_state_paths` was still at the top.
    Only the hardcoded-path check caught it. This closes that hole, so a file
    that imports the helper and forgets to use it fails too.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isolated_state_paths"
        ):
            return True
    return False


def _calls_main(tree: ast.AST) -> bool:
    """`solver_writer.main()` as a real call, not a mention in prose.

    A text search would match this module's own docstring, and it matched two
    files whose only reference to `main()` is a comment explaining where a
    function used to live — which is how the original count of eleven files came
    out two too high.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "main"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "solver_writer"
        ):
            return True
    return False


class TestEveryMainDrivingTestIsolatesItsStatePaths(unittest.TestCase):
    def test_no_main_driving_test_omits_isolation(self):
        offenders = []
        for path in _test_files():
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except (
                SyntaxError
            ) as exc:  # pragma: no cover - a parse error is its own failure
                self.fail(f"{path.name} does not parse: {exc}")
            if not _calls_main(tree):
                continue
            if not _isolates(tree):
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            "these drive the real solver_writer.main(), whose persisted paths "
            "default to real /opt production files, without redirecting them "
            "(nimbus #1330). Add `isolated_state_paths(solver_writer)` to the "
            "with-block -- see tests/_isolated_state.py: " + ", ".join(offenders),
        )

    def test_at_least_one_file_is_actually_checked(self):
        """Guard against the guard passing vacuously.

        If `_calls_main` ever stopped matching -- a rename, a refactor behind a
        helper -- the loop above would examine nothing and pass.
        """
        driving = [
            p.name
            for p in _test_files()
            if _calls_main(ast.parse(p.read_text(encoding="utf-8")))
        ]
        self.assertGreaterEqual(
            len(driving),
            5,
            f"expected several main()-driving test files, found {driving}",
        )


class TestTheHardcodedPathConventionIsGone(unittest.TestCase):
    """The `/tmp/nonexistent_*.json` pattern, and any new copy of it."""

    def test_no_test_hardcodes_a_persisted_state_path(self):
        offenders = []
        for path in _test_files():
            src = path.read_text(encoding="utf-8")
            if not _calls_main(ast.parse(src)):
                continue
            for const in PERSISTED_PATH_CONSTANTS:
                # A literal path handed to one of these constants in a
                # main()-driving test is the old convention returning.
                idx = src.find(f'"{const}"')
                if idx == -1:
                    continue
                window = src[idx : idx + 200]
                if '"/tmp/' in window or '"/opt/' in window:
                    offenders.append(f"{path.name}:{const}")
        self.assertEqual(
            offenders,
            [],
            "a hardcoded /tmp or /opt path for a persisted state constant in a "
            "main()-driving test. /tmp/nonexistent_*.json is not nonexistent "
            "once main() has written it, which is nimbus #1330's first symptom: "
            + ", ".join(offenders),
        )


class TestTheHelperCoversEveryPersistedPath(unittest.TestCase):
    """If a fifth persisted path is added, isolation must extend to it.

    `PERSISTED_PATH_CONSTANTS` is the helper's own list, so this compares it
    against `solver_writer`'s real module-level constants rather than against a
    second copy that could drift.
    """

    def test_the_helper_knows_about_every_path_constant(self):
        writer = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "nimbus_load"
            / "solver_writer.py"
        )
        tree = ast.parse(writer.read_text(encoding="utf-8"))
        declared = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.endswith("_PATH"):
                    continue
                # An os.environ.get(...) default naming a real file, which is
                # what makes it a persisted path rather than a directory or a
                # computed value.
                if isinstance(node.value, ast.Call):
                    declared.add(target.id)
        # TOKEN_PATH is read-only credentials, never written by main().
        declared.discard("TOKEN_PATH")
        missing = sorted(declared - set(PERSISTED_PATH_CONSTANTS))
        self.assertEqual(
            missing,
            [],
            "solver_writer declares these persisted path constants that "
            "isolated_state_paths() does not redirect, so a main()-driving test "
            "would still write the real file: " + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
