"""`noop_patches.py`'s static direct-assignment report (nimbus issue #1434).

## Why this exists

The gate's runtime hooks are `unittest.mock._patch.__enter__` and
`MonkeyPatch.setattr`. A plain attribute assignment on a module object goes
through neither, so for a name like `_NATIVE_HASS` the gate observes **9 of 201
sites** and, before this, had no way to say so.

That silence has already produced two documented errors. Spec 007's inventory
recorded `LOCK_PATH` as having **0 patch sites** when it has four -- one of them
behind an `if hasattr(...)` guard that would have made a missed site *silent*.
And #1305's own analysis cleared four names after checking one and generalising,
which is the same mistake with a different shape.

So this is deliberately a **count, not a verdict**: it cannot say whether an
assignment was read, only that the gate cannot see it. A reviewer weighing "can
I move this name?" needs the number first.

## The heuristic, and the one case that forced it

Reporting `X.attr = value` whenever `X` matches a module basename is wrong:
`coordinator.data = {...}` is a local variable in three test files that never
import `coordinator` as a module -- measured, **6 false positives**. So the base
name must be one the file itself binds to a package module.

Tightening it that way also *found* sites, which is the part worth pinning:
`asname` imports (`from ... import forecast_snapshot_store as fss`, then
`fss._store_for = ...`) were invisible to the loose version. Both directions are
asserted below, because a heuristic that only ever loses matches when narrowed
would be indistinguishable from one that is simply broken.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "gates"))

from noop_patches import (
    REPO_ROOT,
    _integration_module_names,
    _static_direct_assignment_sites,
)


class TestItFindsTheRealBlindSpot(unittest.TestCase):
    def test_the_module_name_set_is_derived_from_the_tree_not_hardcoded(self):
        names = _integration_module_names(REPO_ROOT)
        self.assertGreater(len(names), 40, f"suspiciously few modules: {len(names)}")
        # Modules from three different extraction phases, so a future phase that
        # adds a package is picked up without editing the tool.
        for expected in ("solver_writer", "solver_shared", "solver_runtime"):
            self.assertIn(expected, names)
        self.assertNotIn("__init__", names)

    def test_it_reports_the_name_this_issue_was_filed_about(self):
        """Non-vacuity, against the real tree. `_NATIVE_HASS` is the whole
        reason #1434 exists: 192 assignments the gate cannot see."""
        sites = _static_direct_assignment_sites(REPO_ROOT)
        flat = [h for hits in sites.values() for h in hits]
        native = [h for h in flat if h.endswith("solver_writer._NATIVE_HASS")]
        self.assertGreater(
            len(native),
            100,
            "expected the ~192 solver_writer._NATIVE_HASS assignments; got "
            f"{len(native)}. If this dropped, either the suite genuinely "
            "retargeted them (check before editing this number) or the scan "
            "regressed.",
        )

    def test_it_does_not_report_a_local_variable_that_shares_a_module_name(self):
        """The case that forced the import-binding check. `coordinator.py` is a
        real module, so a loose scan reported `coordinator.data = {...}` in three
        files that never import it -- 6 false positives."""
        sites = _static_direct_assignment_sites(REPO_ROOT)
        flat = [h for hits in sites.values() for h in hits]
        self.assertEqual(
            [h for h in flat if h.endswith("coordinator.data")],
            [],
            "a local variable named after a module is being reported as a "
            "module-attribute assignment",
        )

    def test_it_sees_an_asname_module_import(self):
        """`from ... import forecast_snapshot_store as fss` then
        `fss._store_for = ...` is a real direct assignment on a real module, and
        the loose version of this scan missed it entirely."""
        sites = _static_direct_assignment_sites(REPO_ROOT)
        flat = [h for hits in sites.values() for h in hits]
        self.assertTrue(
            any(h.endswith("fss._store_for") for h in flat),
            "an aliased module import's assignment is not being reported",
        )

    def test_hass_integration_is_excluded_matching_the_tools_own_selection(self):
        """That directory needs the real HA harness this tool does not drive --
        the same exclusion `_list_candidate_test_files()` documents."""
        sites = _static_direct_assignment_sites(REPO_ROOT)
        self.assertEqual(
            [rel for rel in sites if "hass_integration" in rel],
            [],
        )

    def test_it_fails_open_on_a_tree_with_no_package(
        self,
    ):
        """Reporting must never be the thing that breaks the gate."""
        self.assertEqual(
            _static_direct_assignment_sites(pathlib.Path("/nonexistent")), {}
        )
        self.assertEqual(_integration_module_names(pathlib.Path("/nonexistent")), set())


if __name__ == "__main__":
    unittest.main()
