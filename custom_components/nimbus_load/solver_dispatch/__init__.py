"""Dispatch stage of the Solver cycle (nimbus issue #1305, Phase 6 of #1298).

One module, `guard.py`, holding `apply_commanded_state_guard()` -- the only code
in Nimbus that turns a plan into a real service call against household hardware
(climate entities, relays, EV chargers), subject to the activation-allowed,
min-hold, max-activations-per-day, relay-chatter and done-condition guards.

**This is the highest-consequence module in the package.** #1298 scheduled it
last on cited evidence: #726, #741, #733, #769, #770/#782 and the shared-charger
widening bug fixed in #1140 all lived in this guard logic. Phase 6 relocated it
and changed nothing else -- strip every inserted `sw.` and each moved block is
byte-for-byte what it was in `solver_writer.py`.

## The import direction, which is a correctness property and not a style choice

Identical to the seam `solver_inputs/__init__.py` and `solver_reports/
__init__.py` document at length. **Read one of those before changing anything
here.** In short: this module reaches back into `solver_writer` via a DEFERRED,
in-function, by-MODULE import (`sw = _solver_writer()`, then `sw.name`), never
`from ..solver_writer import name`. Two reasons -- `solver_writer` imports this
package at module scope, so a module-scope import back is circular; and the
suite patches these names as attributes on the `solver_writer` module object,
which only a call-time attribute lookup respects.

Measured for THIS phase's own names, because the measurement is what makes the
rule load-bearing rather than decorative:

    _resolve_controllable_load_tuning  solver_writer._resolve_controllable_load_
                                       tuning = _maybe_raise (same file, :254) --
                                       a direct ASSIGNMENT, the shape
                                       tests/gates/noop_patches.py structurally
                                       cannot observe (#1400/#1401)
    LOCAL_TZ                           rebound by direct assignment at 4 sites in
                                       test_solver_writer_local_tz_resolution.py

Only three kinds of name are imported directly, because none is ever patched and
all bind stably: the standard library (`datetime`, `timedelta`,
`dataclasses.replace`), third-party (`numpy`, `numpy.typing.NDArray`), and the
pure `solver/` package (`network`).

## `_LOGGER` is the exception, and getting it wrong once is worth recording

`_LOGGER` is reached as **`solver_shared._LOGGER`**, NOT through the seam -- which
is exactly what spec 006's Invariants require, and what this module's first draft
violated.

The reasoning for violating it looked sound.
`test_commanded_state_guard_reports_its_own_failure.py:298` did
`patch.object(solver_writer, "_LOGGER")`, and `solver_writer._LOGGER` is an alias,
so a `Mock` on it is invisible to code reading `solver_shared`. Using the seam
made that test pass.

It also broke a **package-wide gate**:
`test_callers_mode_counts_only_real_references.py::test_the_real_tree_now_finds_
zero_logger_callers` asserts **zero** real seam-shaped `_LOGGER` references
anywhere outside `solver_writer.py` -- Phase 2a's own success condition, since
`_LOGGER` has lived in `solver_shared.py` since then. And
`solver_inputs/controllable_loads.py`'s own module docstring already warns future
phases, having hit this in Phase 3, under the heading

    ## `_LOGGER` is `solver_shared._LOGGER`, never the seam form

So the right fix was not to bend the rule for this module. It was to patch the
logger where it actually lives: that one test now patches
`solver_shared._LOGGER`, and both it and the package gate pass.

A second, mechanical consequence worth knowing: reading `_LOGGER` through
`solver_shared` means ruff can no longer trace it as a logger, so the seven
`# noqa: BLE001` directives on the blind-`except` handlers are needed again --
and are therefore the originals, unedited, which is one fewer difference from the
pre-move source.

## Layer position

Layer 2 in the `nimbus-layers` contract: below `solver_writer`, above
`solver_inputs` / `solver_shared` / `solver`. Nothing here imports upward except
through the deferred seam above.
"""
