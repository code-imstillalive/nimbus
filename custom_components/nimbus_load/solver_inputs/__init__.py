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

Both reasons dissolve once #735's own `solver/ha_bridge.py` step lands
and those helpers have a home that doesn't depend on `solver_writer` --
at which point these deferred imports become plain module-scope ones.
Until then they are the honest cost of doing this in reversible stages
instead of one large move.
"""

from __future__ import annotations
