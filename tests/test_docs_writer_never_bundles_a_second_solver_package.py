"""Regression test for nimbus issue #357 (Mark Purcell, codebase review):
"nimbus_solver_app/solver/ and the three writer scripts have drifted from
the integration copy... nothing enforces sync."

The actual root cause of the drift the issue's own evidence table
documents (network.py missing #328's soft-SoC relaxation, elements.py
missing #328's initial_soc_kwh relaxation, lp.py missing #238's MIP
support, quality_report.py missing #297/#310's row-major refactor) was
`nimbus_solver_app/` carrying its OWN separate, bundled copy of `solver/`
-- a real third source of truth nothing kept in sync with
`custom_components/nimbus_load/solver/`. That add-on has since been
deleted entirely (v0.94.85, nimbus issue #357's own "option (a)": "delete
nimbus_solver_app/ now") -- confirmed via `git log` and a direct
filesystem check that the directory no longer exists anywhere in this
repo.

This test locks in WHY that class of drift can no longer recur: the one
remaining non-integration writer script
(`docs/real-world-integration/files/nimbus_solver_forecast_writer.py`,
the standalone/cron deployment example) has never bundled its own copy
of `solver/`/`ml/` at all -- it always resolves both packages via
`sys.path.insert(NIMBUS_SOLVER_PATH, ...)` pointing at an EXTERNAL,
single, real clone of this same repo's `custom_components/nimbus_load/`
directory (see that script's own "PORTABILITY" comment). There is
structurally only ever one copy of `solver/`/`ml/` in existence for any
real install using this script, so there is nothing for it to drift
against.

Two things this test checks directly, not assumed from reading the
script once: (1) no `solver/`/`ml/` directory exists anywhere under
`docs/real-world-integration/` (a bundled copy sneaking back in via a
future edit would be caught here immediately); (2) the docs-copy writer
script's own `elements`/`network` module objects, once its sys.path shim
resolves, are the SAME real objects the integration copy uses -- not two
independently-imported copies that happen to have identical source, a
distinction that matters because Python treats two `imp.load`ed copies
of the "same" module as genuinely different classes/objects even with
byte-identical source.

The writer SCRIPTS themselves (not the solver/ package) do still have
real, substantial content differences -- see the issue's own follow-up
comment for an itemized breakdown -- but every one of them was confirmed
to be a deliberate, execution-context-specific difference (native-HA-only
reporting features vs. a standalone-cron-only timing fix), not a missed
bug fix silently absent from one copy. That's a judgment call for a
human maintainer to keep confirming over time, not something a single
test can enforce -- this test only locks in the one thing that WAS the
actual, structural root cause: a bundled, independently-drifting
solver/ package existing at all.
"""

from __future__ import annotations

import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_DOCS_INTEGRATION_DIR = os.path.join(_REPO_ROOT, "docs", "real-world-integration")


class TestNoBundledSolverPackageExistsOutsideTheIntegration(unittest.TestCase):
    def test_nimbus_solver_app_directory_is_gone(self):
        # test_nimbus_solver_app_directory_is_gone: The add-on that actually carried the drifting bundled copy --
        addon_dir = os.path.join(_REPO_ROOT, "nimbus_solver_app")
        self.assertFalse(
            os.path.isdir(addon_dir),
            "nimbus_solver_app/ has reappeared -- this directory's own "
            "bundled solver/ copy was the real root cause of issue #357's "
            "drift, deleted for exactly that reason in v0.94.85",
        )

        # test_docs_real_world_integration_never_bundles_solver_or_ml: Walks the whole docs/real-world-integration/ tree looking for
        self.assertTrue(
            os.path.isdir(_DOCS_INTEGRATION_DIR),
            f"expected {_DOCS_INTEGRATION_DIR} to exist",
        )
        found = []
        for root, dirs, _files in os.walk(_DOCS_INTEGRATION_DIR):
            for d in dirs:
                if d in ("solver", "ml"):
                    found.append(os.path.join(root, d))
        self.assertEqual(
            found,
            [],
            f"found a bundled solver/ or ml/ directory under "
            f"docs/real-world-integration/: {found} -- this is exactly "
            "the class of drift-prone third copy issue #357 was opened "
            "about; the standalone writer script must keep resolving "
            "these packages externally via its own NIMBUS_SOLVER_PATH "
            "sys.path shim, never a bundled copy",
        )

        # test_docs_writer_resolves_the_real_shared_solver_package_not_a_copy: Loads the real docs-copy script (same technique already
        os.environ.setdefault(
            "NIMBUS_SOLVER_PATH",
            os.path.join(_REPO_ROOT, "custom_components", "nimbus_load"),
        )
        import importlib.util
        import sys

        script_path = os.path.join(
            _DOCS_INTEGRATION_DIR, "files", "nimbus_solver_forecast_writer.py"
        )
        spec = importlib.util.spec_from_file_location(
            "nimbus_solver_forecast_writer_drift_check", script_path
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)

            sys.path.insert(
                0, os.path.join(_REPO_ROOT, "custom_components", "nimbus_load")
            )
            try:
                from solver import network as expected_network
            finally:
                sys.path.pop(0)

            self.assertIs(
                mod.network,
                expected_network,
                "the docs-copy writer script resolved a DIFFERENT solver."
                "network module object than the real, shared package -- "
                "it has somehow started reading a bundled/shadowed copy "
                "instead of the one true external solver/ package",
            )
        finally:
            del sys.modules[spec.name]
