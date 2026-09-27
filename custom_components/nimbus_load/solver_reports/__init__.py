"""Reporting stage of the Solver cycle (nimbus issue #1301, Phase 2 of #1298).

These modules hold the HA-facing *orchestration and publish* wrapper around
the reporting math -- they do not hold the math. That already lives in the
pure, HA-import-free `solver/` package (`solver/quality_report.py`,
`solver/regret.py`, `solver/epr.py`), and this phase does not touch it. What
moves here is the part that reads recorder history, assembles a payload and
publishes a sensor. Read-only computations; no dispatch surface at all.

- `backtest.py`       -- compute_efficiency_backtest_report
- `counterfactual.py` -- compute_nimbus_only_soc_counterfactual
- `flex.py`           -- _compute_flex_report_for_window
- `quality.py`        -- the quality-report cluster (Phase 2c, not yet moved)

**On the import direction -- the same load-bearing seam `solver_inputs/
__init__.py` documents at length. Read that file before changing anything
here; the reasoning is identical and it is not a style preference.**

Every module here reaches back into `solver_writer` for shared helpers via a
DEFERRED, in-function, by-MODULE import (`sw = _solver_writer()`, then
`sw.helper(...)`), never `from ..solver_writer import helper`. Two reasons:

1. `solver_writer` imports this package at module scope, so a module-scope
   import back would be circular.
2. The suite patches those helpers as attributes on the `solver_writer`
   module object. `sw.helper(...)` resolves the attribute at CALL time, so
   those patches keep working. A module-scope `from` import binds the
   original function object at IMPORT time and silently defeats every one
   of them -- the test then exercises the real helper and, for the I/O ones,
   attempts a live HTTP request.

Measured for THIS phase's own moved functions rather than assumed, because
the counts are what make the rule load-bearing:

    fetch_entity_history_range   patched on solver_writer in 12 test files
    _LOGGER                      patched on solver_writer in  6 test files
    _kw_scale_factor             patched on solver_writer in  1 test file

Note `_LOGGER` in particular. `solver_inputs/solar.py` reaches it as
`solver_shared._LOGGER`, which is correct there because nothing patches
`solver_writer._LOGGER` on those paths. It is NOT correct here: six test
files do patch it, and `solver_writer._LOGGER` is an identity alias of
`solver_shared._LOGGER` (see PR #1350), so a `Mock` installed on the
`solver_writer` attribute is invisible to code reading the `solver_shared`
one. These modules therefore use `sw._LOGGER`, which reproduces exactly the
resolution the code had while it still lived in `solver_writer`.

Only three kinds of name are imported directly, because none of them is
ever patched and all bind stably: the standard library (`datetime`,
`timedelta`), third-party (`numpy`), and the pure `solver/` package modules
(`elements`, `lp`, `network`). Everything else that today resolves in
`solver_writer`'s namespace is reached via `sw.`.

A constant whose ONLY references are inside a function that moves here
travels with that function and keeps a re-export alias in `solver_writer`,
the same way PR #1350 handled `solver_shared.py`'s constants. That was
checked per constant, not assumed -- `FLEX_SIGNALS_ENTITY_ID` and
`_PRICE_BAND_WIDTH` have no other caller in `solver_writer`, and no test
patches either.
"""

from __future__ import annotations
