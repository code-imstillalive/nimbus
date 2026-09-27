#!/usr/bin/env python3
"""Regenerates tests/golden_1310/reference_0d0d553a.json -- nimbus issue
#1310's pre-extraction reference capture.

Reference revision:

    0d0d553a22e8e4d5db2339ede13337d1645fa220

    (the commit immediately before PR #1308 / nimbus issue #1300 moved
    build_extra_batteries() and _resolve_battery_participant_history()
    out of solver_writer.py into solver_inputs/extra_batteries.py and
    solver_inputs/battery_participants.py -- both functions still live
    directly on solver_writer at this commit.)

Reproduction command, run from a git worktree checked out AT that commit
(so the two are never on disk in the same repo state at once):

    git worktree add --detach /tmp/nimbus-1310-ref 0d0d553a22e8e4d5db2339ede13337d1645fa220
    cp tests/golden_1310/scenarios.py tests/golden_1310/capture.py \\
        /tmp/nimbus-1310-ref/tests/golden_1310/   # this package doesn't exist yet at that commit
    cd /tmp/nimbus-1310-ref
    python3 tests/golden_1310/capture.py tests/golden_1310/reference_0d0d553a.json
    cp tests/golden_1310/reference_0d0d553a.json <this-repo>/tests/golden_1310/
    git worktree remove /tmp/nimbus-1310-ref

This script deliberately does not import solver_inputs itself -- it tries
that layout first (so it can also be re-run against current main as a
sanity check) and falls back to the pre-extraction layout, where both
target functions are plain attributes of solver_writer. Either way the
SAME scenarios.py drives the run, which is what makes the two captures
comparable at all.
"""

from __future__ import annotations

import json
import os
import sys


def _add_solver_path() -> None:
    """Same append-not-insert reasoning as tests/_solver_path.py: this
    repo's own custom_components/nimbus_load/select.py would otherwise
    shadow the stdlib `select` module used deep inside socket/asyncio."""
    tests_dir = os.path.dirname(os.path.abspath(__file__))  # .../tests/golden_1310
    tests_parent = os.path.dirname(tests_dir)  # .../tests
    repo_root = os.path.dirname(tests_parent)  # repo root
    solver_parent = os.path.join(repo_root, "custom_components", "nimbus_load")
    if solver_parent not in sys.path:
        sys.path.append(solver_parent)
    if tests_parent not in sys.path:
        sys.path.insert(0, tests_parent)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: capture.py <out.json>", file=sys.stderr)
        return 2
    out_path = argv[0]

    _add_solver_path()
    import solver_writer
    from golden_1310 import scenarios

    try:
        from solver_inputs import battery_participants as bp_mod
        from solver_inputs import extra_batteries as eb_mod

        build_fn = eb_mod.build_extra_batteries
        resolve_fn = bp_mod._resolve_battery_participant_history
        layout = "post-extraction (solver_inputs package)"
    except ImportError:
        build_fn = solver_writer.build_extra_batteries
        resolve_fn = solver_writer._resolve_battery_participant_history
        layout = "pre-extraction (solver_writer.py)"

    record = scenarios.capture(solver_writer, build_fn, resolve_fn)
    record["_layout"] = layout
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(record, f, sort_keys=True, indent=1)
        f.write("\n")
    print(f"wrote {out_path} ({layout})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
