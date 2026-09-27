#!/usr/bin/env python3
"""Part C gate: the mypy ratchet.

docs/specs/000-golden-master-and-gates.md Part C: "mypy ratchet | the
advisory `typecheck` job's count, which may not rise." The advisory job is
`.github/workflows/ci.yml`'s "Type Check (mypy) [advisory]" job:

    mypy custom_components/nimbus_load --ignore-missing-imports || true

The `|| true` means a red mypy run there never fails CI -- it is purely
informational. This script is what turns "the count may not rise" from a
sentence in a spec into something that can actually fail a build: it runs
the SAME command (see _MYPY_ARGS below -- keep this in lockstep with the
workflow file, not a close approximation of it), counts mypy's own reported
errors, compares the count against a stored baseline, and exits non-zero if
the count has gone UP.

## Baseline provenance

tests/gates/mypy_baseline.txt was seeded by running this script for real
against `custom_components/nimbus_load` on 2026-09-27, at
`origin/main`'s tip (commit 0fc9227, "Spec 000: golden master for
main()..."), using mypy 2.3.1 and Python 3.13.12 (this repo's own
`requires-python` floor is 3.14.4; 3.14 was not available in the sandbox
this was measured in, and mypy's own typeshed selection can differ by
Python minor version -- see the note below). Real count: 155.

## Two known, real, undodged limitations

1. **The CI advisory job installs mypy UNPINNED** (`pip install mypy
   numpy`, no version constraint) and can silently pick up a newer mypy
   release at any time, independent of any code change in this repo. This
   script's own dev-extra dependency pins `mypy==2.3.1` exactly (see
   pyproject.toml's own comment on that pin) specifically so the count
   this script measures is reproducible -- but that means the pinned
   count here and the advisory job's own live count can drift apart over
   time for reasons that have nothing to do with this repository's code.
   Promoting this script into that advisory job (out of this PR's scope)
   would need the job's own mypy install pinned to match, or the ratchet
   becomes noise.
2. **mypy's own typeshed/stdlib-stub selection depends on the Python
   version RUNNING it**, not just mypy's own release, absent an explicit
   `--python-version` flag (which the CI job does not pass, so this
   script does not add one either -- matching CI exactly is the whole
   point). The baseline above was measured on 3.13.12; CI's own "Unit
   Tests (pytest)" job (which is what actually runs this script's own
   tests/test_gates_mypy_ratchet.py) runs Python 3.14. A real 3.14-vs-3.13
   count difference, if one exists, has not been verified from this
   sandbox.

Because of both points, this script is deliberately NOT wired into an
automatic "the real repo's count must never exceed the baseline" pytest
assertion in this PR -- tests/test_gates_mypy_ratchet.py proves the
SCRIPT's own logic against small, unambiguous, environment-insensitive
synthetic fixtures (a clean module, a module with two obvious real type
errors) instead. Wiring a real-repo hard-fail assertion into the normal
suite is a natural next step once someone has confirmed the baseline
holds on CI's actual Python 3.14 runner, not assumed from a 3.13 sandbox.

Usage:
    python tests/gates/mypy_ratchet.py [--baseline N] [--baseline-file PATH]
        [--target PATH] [--update-baseline]

    --baseline N          Compare against this integer instead of reading
                           --baseline-file. Mainly for
                           tests/test_gates_mypy_ratchet.py's synthetic
                           cases; the real gate (no arguments) always reads
                           the stored file.
    --baseline-file PATH  Where the baseline count lives (default:
                           tests/gates/mypy_baseline.txt, beside this
                           script).
    --target PATH         What to run mypy against (default:
                           custom_components/nimbus_load, matching CI's
                           advisory job exactly). Overridable so tests can
                           point this at a fixture module instead of the
                           real package.
    --update-baseline     Overwrite --baseline-file with the count just
                           measured, regardless of direction. For a
                           deliberate cleanup PR that wants to ratchet the
                           count down -- see the plan's own gate table:
                           "mypy ratchet | error count never rises".
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_TARGET = "custom_components/nimbus_load"
_DEFAULT_BASELINE_FILE = Path(__file__).resolve().parent / "mypy_baseline.txt"

# Exactly .github/workflows/ci.yml's "Type Check (mypy) [advisory]" job's
# own invocation, apart from the target path itself (parameterised via
# --target for testability -- see module docstring). A mismatch here would
# make the ratchet compare two different things and pass or fail for the
# wrong reason; if that workflow's own command ever changes, this must
# change with it in the same PR.
_MYPY_ARGS = ("--ignore-missing-imports",)

# mypy's own summary line is one of:
#   "Success: no issues found in N source files"
#   "Found N error in M file (checked K source files)"
#   "Found N errors in M files (checked K source files)"
# Only the error count is what CI's own advisory job would show as red;
# `note:` lines (e.g. overload-variant hints) are supplementary context
# attached to an error already counted, never a separate finding -- verified
# by inspecting real mypy output against this exact repo, where every `note:`
# line sits directly beneath an `error:` line for the same location.
_FOUND_RE = re.compile(r"^Found (\d+) errors?\b", re.MULTILINE)
_CLEAN_RE = re.compile(r"^Success: no issues found\b", re.MULTILINE)


class MypyOutputUnparseable(RuntimeError):
    """mypy's own summary line could not be found in its output.

    Raised rather than silently treated as zero errors -- a ratchet that
    can silently read "0" from output it never actually understood is a
    check that can never fail, the #757 pattern this repo already has a
    name for. A syntax error mypy cannot get past (e.g. running under a
    Python too old to parse this package's own PEP 695 `type` statements)
    lands here rather than being misreported as "clean"."""


def run_mypy(target: str) -> tuple[int, str]:
    """Run mypy against ``target`` exactly as CI's advisory job does.

    Returns (error_count, raw_combined_output).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", target, *_MYPY_ARGS],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr

    if _CLEAN_RE.search(output):
        return 0, output

    match = _FOUND_RE.search(output)
    if match:
        return int(match.group(1)), output

    raise MypyOutputUnparseable(
        "Could not find mypy's own 'Found N error(s)' or 'Success: no "
        "issues found' summary line in its output -- treating this as a "
        "hard failure rather than guessing a count. This usually means "
        "mypy crashed or hit a fatal error (e.g. a syntax construct the "
        "running Python is too old to parse) before it could finish "
        f"checking {target!r}. Raw output:\n{output}"
    )


def read_baseline(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


def write_baseline(path: Path, count: int) -> None:
    path.write_text(f"{count}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="mypy ratchet -- docs/specs/000-golden-master-and-gates.md Part C."
    )
    parser.add_argument(
        "--baseline",
        type=int,
        default=None,
        help="Compare against this integer instead of reading --baseline-file.",
    )
    parser.add_argument(
        "--baseline-file",
        type=Path,
        default=_DEFAULT_BASELINE_FILE,
        help=f"Where the baseline count is stored/updated (default: {_DEFAULT_BASELINE_FILE}).",
    )
    parser.add_argument(
        "--target",
        default=_DEFAULT_TARGET,
        help=f"What to run mypy against (default: {_DEFAULT_TARGET}, matching CI's advisory job).",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Overwrite --baseline-file with the count just measured, regardless of direction.",
    )
    args = parser.parse_args(argv)

    count, output = run_mypy(args.target)

    if args.update_baseline:
        write_baseline(args.baseline_file, count)
        print(
            f"mypy_ratchet: measured {count} finding(s) against {args.target!r}; "
            f"wrote new baseline to {args.baseline_file}."
        )
        return 0

    baseline = (
        args.baseline
        if args.baseline is not None
        else read_baseline(args.baseline_file)
    )

    if count > baseline:
        print(output)
        print(
            f"mypy_ratchet: FAIL -- {count} finding(s) against {args.target!r}, "
            f"baseline is {baseline}. The advisory mypy count rose; fix the new "
            f"finding(s), or, for a deliberate change, rerun with "
            f"--update-baseline to accept the new count."
        )
        return 1

    print(
        f"mypy_ratchet: OK -- {count} finding(s) against {args.target!r}, baseline is {baseline}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
