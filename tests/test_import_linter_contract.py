"""Runs the real import-linter contract as part of the normal test run.

docs/architecture/tech-debt-plan.md section 2 defines the target layer map
for `custom_components/nimbus_load` (solver/ -> solver_inputs+
solver_reports -> solver_plan+solver_publish+solver_dispatch ->
solver_writer+standalone_writer, imports pointing down that list only) and
docs/specs/000-golden-master-and-gates.md Part C lists "import-linter
contracts" as one of the six gates every refactor PR must pass. The
contract itself lives in `[tool.importlinter]` in pyproject.toml, next to
the ruff/pytest config it sits beside -- this file is the "static analysis
wrapped in a pytest assertion" wiring, the same house style tests/
test_docs_writer_function_set_drift.py already uses for a different check,
so a layering regression fails the normal
`pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant` run
instead of needing a separate manual step anyone could forget to run.

What "pass" means today: the contract is configured to KEEP with exactly
the plan's own seven documented `_solver_writer()` late-import violations
recorded as `ignore_imports` exceptions (see pyproject.toml's own comment
block above `[tool.importlinter]` for the full reasoning, including why
`solver/ha_bridge.py` -- the plan's item 4 -- is deliberately NOT one of
the declared layers). Fixing those seven is each future extraction phase's
own job, not this test's; this test only pins that the count of recorded
exceptions doesn't silently grow, and that lint-imports still runs clean
against everything else.

tests/test_gates_import_linter.py proves the underlying mechanism (a
`layers` contract genuinely catches a new violation and genuinely passes a
clean one) against small synthetic fixtures, not this real repo -- this
file is the live gate over the actual codebase; that one is the read on
whether the tool itself, generically, works as import-linter documents it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

# The exact number of `ignore_imports` entries in pyproject.toml's
# `nimbus-layers` contract right now. Pinned so a NEW exception silently
# added (rather than a real fix landing and the matching entry being
# removed) is itself visible in a diff of this file, not just of
# pyproject.toml -- the same "don't let the exceptions list rot silently"
# reasoning the pyproject.toml comment gives for leaving
# `unmatched_ignore_imports_alerting` at its default. When a phase fixes
# one of the seven late imports the plan's section 1 documents, this
# number goes down along with the pyproject.toml entry it's counting.
_EXPECTED_IGNORED_IMPORT_COUNT = 7


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    # `importlinter` ships a `lint-imports` console-script entry point, not
    # a runnable `__main__.py` -- `python -m importlinter` fails with "is a
    # package and cannot be directly executed". Invoking its click command
    # directly via `sys.executable -c` (rather than shelling out to the
    # `lint-imports` script, which would depend on it having landed on
    # PATH/in the same venv bin/ dir as whatever interpreter is running
    # this test) keeps this test tied to the same Python -- and therefore
    # the same installed import-linter -- that `pip install -e '.[dev]'`
    # set up, the same guarantee `sys.executable`-based subprocess calls
    # elsewhere in this repo (e.g. tests/golden/harness.py's
    # `run_isolated`) rely on.
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--config",
            "pyproject.toml",
            "--no-logo",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestImportLinterLayerContract(unittest.TestCase):
    def test_lint_imports_passes(self):
        """The real gate: `lint-imports` against this repo's own
        pyproject.toml config must exit 0. A non-zero exit means either a
        genuinely NEW layering violation was introduced (fix the import,
        don't add another exception), or the contract itself is
        misconfigured (e.g. a missing non-optional layer)."""
        proc = _run_lint_imports()
        self.assertEqual(
            proc.returncode,
            0,
            "lint-imports failed against pyproject.toml's `nimbus-layers` "
            "contract -- either a new import now crosses the layer "
            "boundaries docs/architecture/tech-debt-plan.md section 2 "
            "defines (fix the import), or the contract configuration "
            "itself is broken (e.g. a layer module renamed/moved without "
            "updating pyproject.toml). Full output:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        )

    def test_exactly_the_documented_violations_are_recorded_as_exceptions(self):
        """import-linter reports how many `ignore_imports` entries it
        actually matched in its own summary line, e.g.
        "... KEPT (7 ignored imports)". Pinning that count (rather than
        just "the contract passed") is what catches a new violation being
        silently swept into `ignore_imports` instead of fixed -- the
        contract would still exit 0 in that case, since a matched
        ignore_imports entry always keeps the contract, but the count
        printed here would rise."""
        proc = _run_lint_imports()
        self.assertIn(
            f"({_EXPECTED_IGNORED_IMPORT_COUNT} ignored imports)",
            proc.stdout,
            "The number of ignore_imports entries import-linter actually "
            "matched no longer equals the expected count. If a phase just "
            "fixed one of the plan's seven documented late imports, lower "
            "_EXPECTED_IGNORED_IMPORT_COUNT here AND remove its now-stale "
            "entry from pyproject.toml's `ignore_imports` list (removing "
            "only one of the two would fail this test, or "
            "test_lint_imports_passes above, respectively). If this rose "
            "instead, a new violation was recorded as an exception rather "
            "than fixed -- that's the regression this test exists to "
            f"catch. Full stdout:\n{proc.stdout}",
        )

    def test_no_missing_or_undeclared_layer_warnings(self):
        """A required (non-parenthesised) layer that stops existing, or a
        contract-config typo, shows up as import-linter refusing to even
        build the graph rather than a normal BROKEN contract -- distinct
        failure text, worth asserting on directly so it isn't confused
        with a real layering violation."""
        proc = _run_lint_imports()
        self.assertNotIn("Missing layer", proc.stdout)
        self.assertNotIn("shared descendants", proc.stdout.lower())


if __name__ == "__main__":
    unittest.main()
