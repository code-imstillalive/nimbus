"""Proves the `import-linter` `layers` contract mechanism itself, the way
docs/architecture/tech-debt-plan.md's own migration step 3 asks for every
Part C tool: "one PR each, each proven on a deliberately bad commit."

Deliberately does NOT mutate `custom_components/nimbus_load/` to do this --
that would be a real, if temporary, behaviour change to production code
sitting in a test file, and it would only prove the contract catches THIS
repo's specific seven exceptions, not that the mechanism generalises.
Instead, three tiny synthetic fixture packages under tests/gates/fixtures/
reproduce the same layer-violation SHAPE at the smallest possible scale:

- layer_ok/          a two-layer package with no violation (control case)
- layer_violation/   the identical shape, but the low layer illegally
                      imports the high layer, with NO recorded exception
- layer_ignored/      the identical violation, but recorded via
                      `ignore_imports` -- the same mechanism pyproject.toml's
                      real `nimbus-layers` contract uses for the tech-debt
                      plan's seven documented `_solver_writer()` late imports

Each fixture carries its own `.importlinter` config (root_packages scoped
to just that fixture's own tiny package), so running `lint-imports` with
`cwd` set to the fixture's own directory exercises the real tool against a
real, if minimal, project -- not a mock of it.

tests/test_import_linter_contract.py is the companion file: it runs the
same tool against the REAL pyproject.toml config and the real
`custom_components/nimbus_load` tree. This file answers "does the layers
contract genuinely work"; that one answers "is nimbus's own contract
genuinely passing right now."
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES_DIR = os.path.join(_HERE, "gates", "fixtures")
_LAYER_OK = os.path.join(_FIXTURES_DIR, "layer_ok")
_LAYER_VIOLATION = os.path.join(_FIXTURES_DIR, "layer_violation")
_LAYER_IGNORED = os.path.join(_FIXTURES_DIR, "layer_ignored")


def _run_lint_imports(cwd: str) -> subprocess.CompletedProcess[str]:
    """Same invocation as tests/test_import_linter_contract.py's own
    _run_lint_imports() -- see that file's comment for why this goes
    through `sys.executable -c` rather than a `lint-imports`/`python -m
    importlinter` shell-out."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--config",
            ".importlinter",
            "--no-logo",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class TestLayerContractMechanism(unittest.TestCase):
    def test_a_clean_two_layer_fixture_passes(self):
        proc = _run_lint_imports(_LAYER_OK)
        self.assertEqual(
            proc.returncode,
            0,
            f"the clean control fixture should pass with no violations:\n{proc.stdout}",
        )
        self.assertIn("KEPT", proc.stdout)

    def test_an_unrecorded_violation_fails(self):
        """The low layer illegally importing the high layer, with no
        `ignore_imports` exception for it, is exactly the shape
        docs/architecture/tech-debt-plan.md section 1 documents for the
        real `solver_inputs -> solver_writer` late imports -- reproduced
        here at the smallest possible scale, with no fix recorded."""
        proc = _run_lint_imports(_LAYER_VIOLATION)
        self.assertNotEqual(
            proc.returncode,
            0,
            f"the violation fixture should fail -- import-linter did not "
            f"catch a lower layer importing a higher one:\n{proc.stdout}",
        )
        self.assertIn("BROKEN", proc.stdout)
        self.assertIn(
            "layer_violation.low is not allowed to import layer_violation.high",
            proc.stdout,
        )

    def test_the_same_violation_recorded_as_an_exception_passes(self):
        """The exact mechanism pyproject.toml's real `nimbus-layers`
        contract uses for the tech-debt plan's seven documented late
        imports: a real structural violation, recorded under
        `ignore_imports`, keeps the contract passing without the
        underlying import being fixed."""
        proc = _run_lint_imports(_LAYER_IGNORED)
        self.assertEqual(
            proc.returncode,
            0,
            f"a recorded exception should keep the contract passing "
            f"despite the real violation underneath it:\n{proc.stdout}",
        )
        self.assertIn("KEPT (1 ignored import)", proc.stdout)

    def test_a_new_violation_still_fails_even_with_an_existing_exception(self):
        """The real, load-bearing property: recording one violation as an
        exception does not blanket-whitelist every future violation. A
        SECOND, unrecorded illegal import introduced alongside the
        recorded one must still fail the contract -- proven on a copy of
        layer_ignored/ with a fresh violation added, confirmed to fail,
        then confirmed clean again once the added violation is removed
        (docs/architecture/tech-debt-plan.md's own migration step 3: "one
        PR each, each proven on a deliberately bad commit... confirm the
        contract fails on it, then confirm it's clean again once
        removed")."""
        with tempfile.TemporaryDirectory(prefix="layer_ignored_plus_one-") as tmp:
            fixture_copy = os.path.join(tmp, "layer_ignored")
            shutil.copytree(_LAYER_IGNORED, fixture_copy)

            # Sanity check: the unmodified copy is still clean, so the
            # failure asserted below is caused by the file added next, not
            # by some other, pre-existing difference in the copy.
            clean_proc = _run_lint_imports(fixture_copy)
            self.assertEqual(clean_proc.returncode, 0, clean_proc.stdout)

            bad_import_path = os.path.join(
                fixture_copy, "layer_ignored", "another_low.py"
            )
            with open(bad_import_path, "w", encoding="utf-8") as f:
                f.write(
                    '"""A second low-layer module with its OWN illegal '
                    "import of `high`, not covered by the fixture's single "
                    "`ignore_imports` entry (which names `low`, not "
                    '`another_low`)."""\n\n'
                    "from . import high\n\n"
                    "TRIPLED = high.VALUE * 3\n"
                )
            # A module the layers contract has never heard of is invisible
            # to it (the same reason pyproject.toml's real contract has to
            # list `solver`/`solver_inputs`/etc. by name) -- declare
            # `another_low` as an independent sibling of `low` at the same
            # tier, exactly the `|` syntax the real nimbus-layers contract
            # uses for e.g. `solver_writer | (standalone_writer)`.
            config_path = os.path.join(fixture_copy, ".importlinter")
            with open(config_path, encoding="utf-8") as f:
                config_text = f.read()
            config_text = config_text.replace(
                "    layer_ignored.low\n",
                "    layer_ignored.low | layer_ignored.another_low\n",
            )
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(config_text)

            broken_proc = _run_lint_imports(fixture_copy)
            self.assertNotEqual(
                broken_proc.returncode,
                0,
                "a NEW, unrecorded violation should fail the contract "
                "even though a different violation is already recorded "
                f"as an exception:\n{broken_proc.stdout}",
            )
            self.assertIn(
                "layer_ignored.another_low is not allowed to import layer_ignored.high",
                broken_proc.stdout,
            )
            # The pre-existing, recorded violation is still fine -- only
            # the new one is reported.
            self.assertNotIn(
                "layer_ignored.low is not allowed to import layer_ignored.high",
                broken_proc.stdout,
            )

            os.remove(bad_import_path)
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(
                    config_text.replace(
                        "    layer_ignored.low | layer_ignored.another_low\n",
                        "    layer_ignored.low\n",
                    )
                )
            fixed_proc = _run_lint_imports(fixture_copy)
            self.assertEqual(
                fixed_proc.returncode,
                0,
                f"removing the added violation should make the contract "
                f"pass again:\n{fixed_proc.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
