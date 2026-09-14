"""Input-gathering stage of the Solver cycle (nimbus issue #735, stage 1).

`solver_writer.main()` had grown to ~1737 lines, of which roughly two
thirds was fetch-and-transform work with clear inputs (`cfg`,
`grid_times`) and clear outputs (arrays) -- no LP state, no publish side
effects, no ordering subtleties with the solve itself. That makes it the
mechanically safest third of the function to move, and #735's own triage
picked it as the high-value/low-risk entry point.

This package is deliberately staged, one input source at a time, each
step independently shippable and independently revertible:

- `solar.py` -- solar forecast fetch/blend/live-anchor (landed).
- load, prices, p2p -- not moved yet; still inline in `main()`.

**On the import direction.** Every module here reaches back into
`solver_writer` for the shared HA-bridge helpers (`ha_get`,
`entity_exists`, `resample_forecast`, ...) via a DEFERRED, in-function
import rather than a module-scope one. Two independent reasons, both
real:

1. `solver_writer` imports this package at module scope, so a module-
   scope import back would be circular.
2. The whole test suite patches those helpers as attributes on the
   `solver_writer` module object (`patch.object(solver_writer,
   "ha_get", ...)`). Importing the MODULE and calling `sw.ha_get(...)`
   resolves the attribute at call time, so those patches keep working;
   `from ..solver_writer import ha_get` would bind the original
   function at import time and silently defeat every one of them.

**Do not "clean this up" into a module-scope `from ..solver_writer
import ha_get`.** Reason 1 is temporary -- it would indeed dissolve once
#735's own `solver/ha_bridge.py` step lands. Reason 2 is not, and it is
the load-bearing one.

This was originally written up as a wart to be removed by doing
`ha_bridge` first. That was wrong, and checking it rather than assuming
is what caught it (2026-09-14). Moving these helpers into their own
module does not just relocate them -- it moves their *internal callers*
too, and those callers then resolve each other through the NEW module's
namespace, escaping every `patch.object(solver_writer, ...)` in the
suite.

The concrete case: `entity_exists()` calls `ha_get()` directly. Move
both into `ha_bridge` and a test patching `solver_writer.ha_get` still
mocks direct calls, but `entity_exists()` silently reaches the real
`ha_get` and attempts a live HTTP request. 31 test files patch
`ha_get`, 24 patch `ha_post_state`, 14 patch `fetch_entity_history_
range` -- verified empirically, not reasoned about.

So the deferred, by-MODULE import is not a cost of staging; it is what
keeps those seams intact, and it stays regardless of whether
`ha_bridge` ever lands. If `ha_bridge` is done later it needs to solve
the cross-call seam problem explicitly (a single indirection point that
tests can still patch), not assume relocation is behaviour-preserving
just because the functions are pure moves.

`tests/test_solver_inputs_solar.py::TestDeferredImportSeamIsLoadBearing`
pins this so the pattern can't be quietly refactored away.
"""

from __future__ import annotations
