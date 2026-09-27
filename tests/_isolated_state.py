"""Every file `solver_writer.main()` touches, redirected into a throwaway dir.

## Why this exists (nimbus issue #1330)

`solver_writer`'s four persisted paths all default to a real production
location:

    PLAN_STATE_PATH                    /opt/nimbus_solver_last_plan.json
    SOLAR_DELIVERY_RATIO_PATH          /opt/nimbus_solver_solar_delivery_ratio.json
    LOCK_PATH                          /opt/nimbus_solver_forecast_writer.lock
    LOAD_FORECAST_ERROR_NOTIFIED_PATH  /opt/nimbus_solver_load_forecast_error.txt

That is correct for the standalone/cron deployment, which is what those
defaults are for. It means a test that drives the real `main()` without
redirecting them reads and writes production state.

Measured when this was written, by AST rather than text search: **9 test files
call `solver_writer.main()`. One isolated both data paths, five isolated only
`PLAN_STATE_PATH`, and three isolated none of them.** (A text search said 11 --
two files mention `main()` only in a comment about where a function used to
live. The guard test walks the AST for exactly that reason.)

On CI and on a Windows dev box `/opt` does not exist, every access fails, the
failure is caught, and nothing is visible — which is exactly the property that
let this survive. Run the suite on either NUC and
those tests read the live last-plan and solar-delivery-ratio files into their
assertions and then overwrite them.

Mark Purcell found the first symptom while building the golden-master harness
(#1328): `test_main_golden_output_guardrail.py` pointed `PLAN_STATE_PATH` at a
fixed `/tmp/nonexistent_plan_state_golden_test.json` and `main()` **writes**
that file, so the second run on one machine read the first run's plan back
through the proximal term and `forecast[1]['shadow_price']` moved 0.3 ->
0.2999. The filename asserted the invariant it broke.

(0.3, not 0.2982. The first write-up of this said 0.2982, which is
`forecast[-1]`'s pinned value -- a different, coarser-tier period. Mark's report
gave only the 0.2999, and the baseline was filled in from the wrong row of the
same fixture. Corrected against `_EXPECTED_ATTRS[1]['shadow_price']` itself.)

## Why a context manager rather than a fixture

Most of these call sites are plain module-level helper functions
(`_run_main()`, `_run_main_and_capture()`) or `unittest.TestCase` methods, so
`tmp_path` is not reachable without threading a fixture through every caller.
A context manager drops into the existing `with (...)` block beside the other
`patch.object` entries, which is where a reader already looks for what the
test controls.

## What this does NOT do

It does not change `main()`. A path that is wrong in production is a
production bug; this only stops the test suite from being one of the writers.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from types import ModuleType
from unittest.mock import patch

#: The module-level constants in `solver_writer` that name a file `main()`
#: reads or writes, each defaulting to a real `/opt` production path.
#:
#: Kept in one place so the guard test (`test_1330_main_driving_tests_isolate_
#: state_paths.py`) and this helper cannot disagree about what the set is, and
#: so a fifth persisted path added later is isolated by editing one tuple.
PERSISTED_PATH_CONSTANTS = (
    "PLAN_STATE_PATH",
    "SOLAR_DELIVERY_RATIO_PATH",
    "LOCK_PATH",
    "LOAD_FORECAST_ERROR_NOTIFIED_PATH",
)

#: Basenames inside the temp dir. Deliberately the same names production uses:
#: a path that appears in a failure message or a leftover directory then reads
#: as the thing it stands in for, rather than as an opaque test artefact.
_BASENAMES = {
    "PLAN_STATE_PATH": "nimbus_solver_last_plan.json",
    "SOLAR_DELIVERY_RATIO_PATH": "nimbus_solver_solar_delivery_ratio.json",
    "LOCK_PATH": "nimbus_solver_forecast_writer.lock",
    "LOAD_FORECAST_ERROR_NOTIFIED_PATH": "nimbus_solver_load_forecast_error.txt",
}


@contextlib.contextmanager
def isolated_state_paths(solver_writer: ModuleType) -> Iterator[str]:
    """Point every persisted path at a fresh temp dir; yield that dir.

    The directory exists and is writable, but starts **empty** — so a test
    whose premise is "no previous plan" still gets that (the directory is
    there, the file inside it is not), while a test that writes one can read
    it back within the same block. Both were true of the hardcoded
    `/tmp/nonexistent_*.json` convention on its first run, and only the first.

    The directory and everything in it is removed on exit, so consecutive runs
    are independent by construction rather than by a filename's promise.
    """
    with (
        tempfile.TemporaryDirectory(prefix="nimbus_solver_state_") as tmpdir,
        contextlib.ExitStack() as stack,
    ):
        for const in PERSISTED_PATH_CONSTANTS:
            stack.enter_context(
                patch.object(
                    solver_writer,
                    const,
                    os.path.join(tmpdir, _BASENAMES[const]),
                )
            )
        yield tmpdir
