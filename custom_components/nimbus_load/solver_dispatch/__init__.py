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

    _LOGGER                            patch.object(solver_writer, "_LOGGER") in
                                       test_commanded_state_guard_reports_its_own_
                                       failure.py:298, in a test whose whole
                                       premise is that this guard logs its own
                                       failure WARNING (#1019)
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

**One knowing departure from spec 006.** Its Invariants say every `_LOGGER` use
in the moved code should reach `solver_shared._LOGGER` directly, never
`sw._LOGGER`. That is wrong here: `solver_writer._LOGGER` is an identity alias of
`solver_shared._LOGGER` (PR #1350), so a `Mock` installed on the `solver_writer`
attribute is invisible to code reading the `solver_shared` one -- and the test
above would then fail against a guard that logs perfectly well. Recorded on
#1305 rather than silently diverging.

## Layer position

Layer 2 in the `nimbus-layers` contract: below `solver_writer`, above
`solver_inputs` / `solver_shared` / `solver`. Nothing here imports upward except
through the deferred seam above.
"""
