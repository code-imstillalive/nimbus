"""Proves tests/gates/mypy_ratchet.py's own logic, the way
docs/architecture/tech-debt-plan.md's own migration step 3 asks for every
Part C tool: "one PR each, each proven on a deliberately bad commit."

Two tiny synthetic fixture modules under tests/gates/fixtures/ back this:

- mypy_ratchet_ok/clean_module.py    zero mypy findings, on any reasonably
                                      current mypy release
- mypy_ratchet_bad/bad_module.py     two unambiguous, real type errors
                                      (an int annotation assigned a str; a
                                      str argument to an int parameter) --
                                      deliberately not subtle, so the
                                      verdict does not depend on mypy-
                                      version or Python-minor-version
                                      differences in typeshed the way a
                                      borderline Optional-narrowing case
                                      might.

Both fixtures are checked with an EXPLICIT `--baseline`, never by reading
tests/gates/mypy_baseline.txt (the real repo's own baseline) -- these tests
exercise the SCRIPT's comparison/counting/parsing logic in isolation, not
the real repo's current mypy health, which tests/gates/mypy_ratchet.py's
own module docstring explains is deliberately NOT asserted on automatically
here (a real Python-minor-version-driven typeshed difference between this
sandbox's 3.13 and CI's 3.14 has not been verified, and CI's own advisory
job installs mypy unpinned, so a hard real-repo assertion in the normal
suite could fail for reasons that have nothing to do with a change under
review). See that docstring for the full reasoning and for the real,
measured baseline (155, on mypy 2.3.1 / Python 3.13.12 / commit 0fc9227).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_RATCHET = os.path.join(_HERE, "gates", "mypy_ratchet.py")
_OK_TARGET = os.path.join(_HERE, "gates", "fixtures", "mypy_ratchet_ok")
_BAD_TARGET = os.path.join(_HERE, "gates", "fixtures", "mypy_ratchet_bad")


def _run_ratchet(*extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, _RATCHET, *extra_args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestMypyRatchetGoodCase(unittest.TestCase):
    def test_clean_fixture_at_zero_baseline_passes(self):
        proc = _run_ratchet("--target", _OK_TARGET, "--baseline", "0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK -- 0 finding(s)", proc.stdout)

    def test_bad_fixture_at_a_generous_baseline_passes(self):
        """The count itself is unchanged; only whether it EXCEEDS the
        baseline matters. A baseline at or above the real count must
        still pass -- this is the "ratchet only tightens, never demands
        perfection it wasn't given" half of the contract."""
        proc = _run_ratchet("--target", _BAD_TARGET, "--baseline", "2")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK -- 2 finding(s)", proc.stdout)

        generous_proc = _run_ratchet("--target", _BAD_TARGET, "--baseline", "50")
        self.assertEqual(
            generous_proc.returncode, 0, generous_proc.stdout + generous_proc.stderr
        )


class TestMypyRatchetBadCase(unittest.TestCase):
    def test_bad_fixture_exceeding_a_low_baseline_fails(self):
        """The real fixture case the task asks for: a fake baseline
        claiming a low count, run against a module with real type errors
        exceeding it."""
        proc = _run_ratchet("--target", _BAD_TARGET, "--baseline", "0")
        self.assertEqual(
            proc.returncode,
            1,
            f"a module with real type errors, checked against a baseline "
            f"of 0, should fail:\n{proc.stdout}{proc.stderr}",
        )
        self.assertIn("FAIL -- 2 finding(s)", proc.stdout)
        # The real mypy diagnostics should be visible in the failure
        # output, not swallowed -- a ratchet that only prints a bare
        # number gives a future reader nothing to act on.
        self.assertIn("Incompatible types in assignment", proc.stdout)

    def test_a_fake_baseline_file_with_a_low_count_also_fails(self):
        """Same as the case above, but via --baseline-file (a real file
        on disk claiming a low count) rather than --baseline (a bare
        integer) -- proving the file-reading path, not just the
        CLI-override path, correctly triggers a failure."""
        with self._temp_baseline_file(count=0) as baseline_path:
            proc = _run_ratchet(
                "--target", _BAD_TARGET, "--baseline-file", baseline_path
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("FAIL -- 2 finding(s)", proc.stdout)

    def _temp_baseline_file(self, count: int):
        import contextlib
        import tempfile

        @contextlib.contextmanager
        def _make():
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(f"{count}\n")
                path = f.name
            try:
                yield path
            finally:
                os.remove(path)

        return _make()


class TestMypyRatchetUpdateBaseline(unittest.TestCase):
    def test_update_baseline_writes_the_measured_count_and_then_passes(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mypy_ratchet_baseline-") as tmp:
            baseline_path = os.path.join(tmp, "baseline.txt")
            with open(baseline_path, "w", encoding="utf-8") as f:
                f.write("999\n")

            update_proc = _run_ratchet(
                "--target",
                _BAD_TARGET,
                "--baseline-file",
                baseline_path,
                "--update-baseline",
            )
            self.assertEqual(
                update_proc.returncode, 0, update_proc.stdout + update_proc.stderr
            )

            with open(baseline_path, encoding="utf-8") as f:
                written = f.read().strip()
            self.assertEqual(
                written,
                "2",
                "--update-baseline should overwrite the file with the "
                "count just measured (2 for the bad fixture), not leave "
                "the old 999 in place.",
            )

            # A normal run against the now-updated baseline passes.
            normal_proc = _run_ratchet(
                "--target", _BAD_TARGET, "--baseline-file", baseline_path
            )
            self.assertEqual(
                normal_proc.returncode, 0, normal_proc.stdout + normal_proc.stderr
            )


class TestMypyRatchetAgainstTheRealRepo(unittest.TestCase):
    """Not a hard gate -- see tests/gates/mypy_ratchet.py's own module
    docstring for why a real-repo pytest assertion isn't wired in yet
    (mypy's typeshed selection can differ by Python minor version, and
    this was only measured on 3.13 locally; CI's "Unit Tests" job that
    runs this file runs 3.14). This test only proves the script runs
    end-to-end against the real target and produces a well-formed,
    parseable result -- it does not assert what that count IS."""

    def test_real_repo_run_produces_a_parseable_result(self):
        proc = _run_ratchet()  # no args: real target, real baseline file
        self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"mypy_ratchet: (OK|FAIL) -- \d+ finding\(s\)")


if __name__ == "__main__":
    unittest.main()
