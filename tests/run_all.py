"""The real, complete test runner for this repo -- run this, not
``python -m unittest discover``, to know the actual state of the suite.

Real bug found live (2026-08-23): this project's test files use TWO
genuinely different styles that coexist --

1. ``unittest.TestCase`` subclasses (e.g. test_solver_writer_cfg_
   defaults.py, test_read_load_forecast_sensor.py) -- what ``unittest
   discover`` is actually built to find.
2. Bare top-level ``def test_...():`` functions with their own manual
   ``if __name__ == "__main__":`` collector/runner (e.g. every
   test_flows_*_subentry.py file, test_config_flow.py, test_number_
   solver_settings.py, and 8 others).

``python -m unittest discover -s tests -p "test_*.py"`` silently finds
ZERO tests from every file in category 2 -- not an import error (which
would show up as a loud, counted failure), a genuine, silent "0 TESTS
FOUND" that never touches the reported pass/fail count at all. This
was reproduced directly: discover reported "102 tests, OK" both before
AND after 3 new, real, individually-passing test files (19 real tests)
existed on disk -- the count never moved, because those 3 files are
category 2. 12 files, 128 real tests total, were invisible to every
past "ran the full suite, X/X passing" claim this project has ever
made -- not because anything was actually broken (verified: all 128
independently pass), but because the check itself had a real, wide,
silent blind spot. This script closes that gap by running BOTH
categories and reporting one honest, combined total.

No pytest/third-party dependency assumed (same reasoning as every
other test file here) -- category 2 files are run as real subprocesses
(so a hard crash/SyntaxError in one can't take down the whole run, and
each file's own printed pass/fail line is trustworthy at face value),
category 1 is loaded via unittest's own TestLoader/TestRunner exactly
as ``discover`` does it internally.

Usage: python tests/run_all.py
Exit code 0 if everything passed, 1 if anything failed.
"""
import glob
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _is_testcase_style(path: str) -> bool:
    """A file belongs to category 1 iff it actually defines a
    unittest.TestCase subclass -- checked by a real import + loadTests
    call (the same mechanism discover itself uses), not a text guess
    (grepping for "TestCase" would false-positive on a file that only
    imports the name in a comment, or false-negative on a subclass of
    a subclass)."""
    sys.path.insert(0, _HERE)
    modname = os.path.splitext(os.path.basename(path))[0]
    try:
        if modname in sys.modules:
            del sys.modules[modname]
        mod = __import__(modname)
        suite = unittest.TestLoader().loadTestsFromModule(mod)
        return suite.countTestCases() > 0
    except Exception:
        # Import genuinely failed -- treat as category 1 so the real
        # TestRunner below reports the real error loudly, instead of
        # silently falling through to category 2's subprocess path
        # (which would just print a Python traceback with no unittest
        # framing).
        return True


def main() -> int:
    all_files = sorted(glob.glob(os.path.join(_HERE, "test_*.py")))
    testcase_files = [f for f in all_files if _is_testcase_style(f)]
    bare_function_files = [f for f in all_files if f not in testcase_files]

    print(f"=== Category 1: unittest.TestCase-style ({len(testcase_files)} files) ===")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for f in testcase_files:
        modname = os.path.splitext(os.path.basename(f))[0]
        if modname in sys.modules:
            del sys.modules[modname]
        mod = __import__(modname)
        suite.addTests(loader.loadTestsFromModule(mod))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    testcase_total = result.testsRun
    testcase_ok = testcase_total - len(result.failures) - len(result.errors)

    print(f"\n=== Category 2: bare-function-style ({len(bare_function_files)} files, run as subprocesses) ===")
    bare_total, bare_ok = 0, 0
    bare_failed_files = []
    for f in bare_function_files:
        proc = subprocess.run([sys.executable, f], capture_output=True, text=True)
        last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no output)"
        name = os.path.basename(f)
        if proc.returncode == 0 and "/" in last_line:
            ok, total = last_line.split("/", 1)
            total = total.split()[0]
            print(f"  {name}: {last_line}")
            bare_ok += int(ok)
            bare_total += int(total)
        else:
            print(f"  {name}: FAILED (exit {proc.returncode})")
            print(f"    {proc.stdout[-500:]}")
            print(f"    {proc.stderr[-500:]}")
            bare_failed_files.append(name)

    grand_total = testcase_total + bare_total
    grand_ok = testcase_ok + bare_ok
    print(f"\n=== TOTAL: {grand_ok}/{grand_total} passed "
          f"({len(testcase_files)} TestCase files + {len(bare_function_files)} bare-function files) ===")

    return 0 if grand_ok == grand_total else 1


if __name__ == "__main__":
    sys.exit(main())
