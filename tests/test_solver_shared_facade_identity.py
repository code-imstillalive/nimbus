"""nimbus issue #1301 (spec 001, #1347) -- solver_shared.py facade identity.

Phase 2a moves 19 named functions/constants (plus the companions their own
callers needed, e.g. `_native_http_error`, `NETWORK_FEE_BLOCK_KEYS`,
`P2P_BLOCK_KEYS`, `parse_iso`, `_nimbus_version`, `_LOGGER`, and the
`_ENTITY_UPDATE_HANDLERS`/`_NATIVE_MANAGED_ENTITY_IDS`/`_MAX_STATE_ATTRS_
BYTES`/`_OVERSIZE_ATTRS_WARNED`/`_SOH_RANGE_WARNED`/`_warn_if_attrs_exceed_
recorder_cap` state each moved function reads) out of `solver_writer.py`
and into a new `solver_shared.py`, verbatim. The façade decision: every
name keeps a re-export in `solver_writer.py`, so every existing caller
(inside this package and any external consumer) keeps working unchanged.

A re-export via `from .solver_shared import X` binds the SAME object into
`solver_writer`'s namespace -- it does not copy or wrap it. This test
proves that directly: `solver_writer.X is solver_shared.X` for every one
of the 28 re-exported names, not just "both resolve to something with the
same name" (a wrapper or a second construction of an equal-but-different
mutable object, e.g. two dicts that compare equal, would pass a `==` check
and fail this `is` check -- exactly the failure mode a facade must not
have, since callers still doing `patch.object(solver_writer, "ha_get",
...)` rely on both names resolving to literally the same function object,
and mutable shared state like `_ENTITY_UPDATE_HANDLERS`/`_SOH_RANGE_WARNED`
relies on both names resolving to literally the same dict/set).

`_LOGGER` specifically: nimbus issue #861 established callers whose OWN
`ha_get`/`ha_post_state`/etc. now resolve through `solver_shared`'s module
globals rather than `solver_writer`'s (see solver_shared.py's own
`_solver_writer()` helper and this file's sibling tests). `_LOGGER` is
different -- `solver_shared.py` creates the one real
`logging.getLogger(__name__)`, and `solver_writer.py` never creates a
second one; it only ever binds the same object under its own name. A
peer-review finding (recorded in solver_shared.py's own module docstring)
confirmed this must be a genuine identity alias rather than two named
loggers, because four existing test suites call `assertLogs(solver_writer.
_LOGGER, ...)` to capture records emitted by code that now lives in
`solver_shared.py` and the `solver_inputs/*.py` modules that import
`solver_shared._LOGGER` directly -- `assertLogs` captures by the logger
OBJECT, not by which module name it is read through, so this only keeps
working if the two names are the same object.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import solver_shared
import solver_writer

# Exactly the names in solver_writer.py's own two `from .solver_shared
# import (...)` / `from solver_shared import (...)` blocks (native and
# standalone/cron paths) -- kept as a literal list, not derived from either
# module's own `dir()`, so a name silently added to one side without the
# other is still a real, visible mismatch rather than something this list
# would quietly absorb.
REEXPORTED_NAMES = (
    "LOCAL_TZ",
    "MAX_TIER1_HOURS",
    "NETWORK_FEE_BLOCK_KEYS",
    "P2P_BLOCK_KEYS",
    "TIER1_PERIOD_HOURS",
    "TIER2_PERIOD_HOURS",
    "_cfg_int",
    "_cfg_num",
    "_ENTITY_UPDATE_HANDLERS",
    "_kw_scale_factor",
    "_local",
    "_LOGGER",
    "_MAX_STATE_ATTRS_BYTES",
    "_native_http_error",
    "_NATIVE_MANAGED_ENTITY_IDS",
    "_nimbus_version",
    "_OVERSIZE_ATTRS_WARNED",
    "_SOH_RANGE_WARNED",
    "_version_stamp",
    "_warn_if_attrs_exceed_recorder_cap",
    "fetch_entity_attribute_history_range",
    "fetch_entity_history_range",
    "fetch_p2p_fixed_export_kw",
    "ha_get",
    "ha_post_state",
    "import_fee_rate",
    "p2p_bonus_price_by_period",
    "parse_iso",
    "resample_history_mean",
    "resample_history_nearest",
    "resolve_effective_capacity_kwh",
)


class TestSolverWriterReexportsAreGenuineAliases(unittest.TestCase):
    def test_every_reexported_name_exists_on_both_modules(self):
        for name in REEXPORTED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(solver_shared, name),
                    f"{name!r} is not defined on solver_shared -- update "
                    "REEXPORTED_NAMES or solver_shared.py, whichever drifted",
                )
                self.assertTrue(
                    hasattr(solver_writer, name),
                    f"{name!r} has no re-export left on solver_writer -- a "
                    "caller resolving this name via `solver_writer.{name}` "
                    "would now raise AttributeError",
                )

    def test_every_reexported_name_is_the_identical_object(self):
        """The core façade guarantee: not equal, not a copy -- the SAME
        object, both by value and by `id()`. This is what makes every
        existing `patch.object(solver_writer, name, ...)` call site (and
        every `solver_writer.<mutable-state>` mutation from code that now
        lives in solver_shared.py) keep behaving exactly as it did before
        the move."""
        mismatches = []
        for name in REEXPORTED_NAMES:
            shared_obj = getattr(solver_shared, name)
            writer_obj = getattr(solver_writer, name)
            if shared_obj is not writer_obj:
                mismatches.append(
                    f"{name}: solver_writer.{name} (id={id(writer_obj)}) is "
                    f"NOT solver_shared.{name} (id={id(shared_obj)})"
                )
        self.assertEqual(
            mismatches,
            [],
            "the following names are no longer genuine aliases:\n"
            + "\n".join(mismatches),
        )

    def test_logger_alias_is_named_after_solver_shared(self):
        """`_LOGGER`'s own real dotted name must be
        `custom_components.nimbus_load.solver_shared` (or the standalone
        equivalent `solver_shared`), never `...solver_writer` -- it is
        created exactly once, in solver_shared.py, via
        `logging.getLogger(__name__)`. Confirming the name directly (not
        just object identity) catches the specific regression this facade
        must avoid: a second, independently-created `_LOGGER =
        logging.getLogger(__name__)` line reappearing in solver_writer.py,
        which would satisfy `is`-identity for every OTHER name in this
        file but silently reintroduce two sibling loggers that do not
        propagate to each other (nimbus issue #861's own logging-hierarchy
        finding)."""
        self.assertTrue(solver_writer._LOGGER.name.endswith("solver_shared"))
        self.assertEqual(solver_writer._LOGGER.name, solver_shared._LOGGER.name)


if __name__ == "__main__":
    unittest.main()
