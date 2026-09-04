"""Regression test for nimbus issue #349 (Mark Purcell, codebase review):
solver_writer.py's own first import used to run blocking file I/O and
heavy imports (numpy, highspy) on the event loop, and an unconditional
sys.path.insert() leaked this package's internal module names
(`ml`, `solver`, `sensor`, `const`, etc.) as TOP-LEVEL names for every
other integration/library in the same HA process.

Three real fixes, each covered here:
1. sensor.py's own first `solver_writer` import already went through
   `hass.async_add_import_executor_job()` (fixed in an earlier session,
   confirmed here via a source-scan so a future edit can't silently
   regress it back to a bare blocking import).
2. solver_writer.py's own internal `.ml`/`.solver` imports now try a
   real relative import FIRST -- resolving cleanly with ZERO sys.path
   mutation when loaded as part of the real
   custom_components.nimbus_load package, and producing the correctly-
   namespaced module object (not a second, distinct top-level `ml`/
   `solver` module). The sys.path shim is only ever reached via the
   ImportError a genuine standalone/cron run raises (no parent package).
3. The REST-mode bearer token is now lazily resolved via `_load_token()`
   instead of a module-level TOKEN_PATH file read at import time --
   native mode never touches disk for this at all.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest

import _solver_path  # noqa: F401
import solver_writer


class TestSensorPyImportsOffTheEventLoop(unittest.TestCase):
    def test_sensor_py_imports_solver_writer_via_the_import_executor(self):
        sensor_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_components",
            "nimbus_load",
            "sensor.py",
        )
        with open(sensor_py, encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "hass.async_add_import_executor_job(",
            src,
            "sensor.py's own first solver_writer import must run on HA's "
            "dedicated import executor, not the event loop",
        )
        self.assertIn(
            'importlib.import_module, ".solver_writer", __package__',
            src,
            "must import the real solver_writer submodule via the "
            "import executor, not some other module/args",
        )


class TestSolverWriterRelativeImportsResolveCleanly(unittest.TestCase):
    def test_real_package_import_needs_no_sys_path_mutation_and_correct_identity(
        self,
    ):
        """Registers bare stub `custom_components`/`custom_components.
        nimbus_load` packages (same technique tests/_ml_path.py already
        uses) so solver_writer.py's real `from .ml.blend import ...`/
        `from .solver import ...` resolve against the REAL package tree,
        without executing the real, HA-dependent __init__.py at all.
        """
        nimbus_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cc_dir = os.path.join(nimbus_root, "custom_components")
        nl_dir = os.path.join(cc_dir, "nimbus_load")

        saved_modules = {
            k: v
            for k, v in sys.modules.items()
            if k == "custom_components" or k.startswith("custom_components.")
        }
        saved_path = list(sys.path)
        try:
            for k in list(saved_modules):
                del sys.modules[k]
            stub_cc = types.ModuleType("custom_components")
            stub_cc.__path__ = [cc_dir]
            sys.modules["custom_components"] = stub_cc
            stub_nl = types.ModuleType("custom_components.nimbus_load")
            stub_nl.__path__ = [nl_dir]
            sys.modules["custom_components.nimbus_load"] = stub_nl

            path_before = list(sys.path)
            mod = importlib.import_module("custom_components.nimbus_load.solver_writer")

            self.assertEqual(
                sys.path,
                path_before,
                "a real package import must not mutate sys.path at all",
            )
            self.assertEqual(
                mod.blend_forecast_array.__module__,
                "custom_components.nimbus_load.ml.blend",
                "the real relative import must produce the correctly-"
                "namespaced module object, not a second, distinct "
                "top-level `ml` module",
            )
        finally:
            for k in list(sys.modules):
                if k == "custom_components" or k.startswith("custom_components."):
                    del sys.modules[k]
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path


class TestTokenIsLazilyResolved(unittest.TestCase):
    def setUp(self):
        solver_writer._TOKEN = None
        solver_writer._TOKEN_LOADED = False

    def tearDown(self):
        solver_writer._TOKEN = None
        solver_writer._TOKEN_LOADED = False

    def test_no_module_level_code_reads_token_path_directly(self):
        """The real regression this issue is about: `TOKEN_PATH` (the
        module-level constant) must never be passed to `open()` outside
        `_load_token()` -- a source-scan, not a reload/re-import trick,
        since reload() would just re-run the module's OWN top-level
        `TOKEN_PATH = os.environ.get(...)` line and defeat any monkey-
        patched sentinel before `_load_token()` ever runs."""
        solver_writer_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_components",
            "nimbus_load",
            "solver_writer.py",
        )
        with open(solver_writer_py, encoding="utf-8") as f:
            src = f.read()
        open_calls_on_token_path = src.count("open(TOKEN_PATH")
        self.assertEqual(
            open_calls_on_token_path,
            1,
            "TOKEN_PATH must be opened in exactly one place: inside "
            "_load_token(), never at module level",
        )
        # The one real call site must be inside _load_token(), not some
        # other, newly-added module-level statement -- confirmed by
        # position: it must appear strictly after that function's own
        # def line.
        def_index = src.index("def _load_token()")
        open_index = src.index("open(TOKEN_PATH")
        self.assertGreater(
            open_index,
            def_index,
            "the real open(TOKEN_PATH) call must live inside _load_token()",
        )

    def test_load_token_resolves_from_env_var_first(self):
        os.environ["HA_TOKEN"] = "test-token-value"
        try:
            self.assertEqual(solver_writer._load_token(), "test-token-value")
        finally:
            del os.environ["HA_TOKEN"]

    def test_load_token_is_cached_after_first_call(self):
        os.environ["HA_TOKEN"] = "first-value"
        try:
            first = solver_writer._load_token()
        finally:
            del os.environ["HA_TOKEN"]
        # Even though the env var is now gone, the cached value stands --
        # same "resolved once per process" contract the original
        # module-level TOKEN had.
        os.environ["HA_TOKEN"] = "second-value"
        try:
            second = solver_writer._load_token()
        finally:
            del os.environ["HA_TOKEN"]
        self.assertEqual(first, "first-value")
        self.assertEqual(second, "first-value")
