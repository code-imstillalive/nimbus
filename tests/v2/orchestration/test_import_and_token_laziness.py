"""Importing the writer runs no blocking I/O or heavy import on the event loop
and leaks no module names (#349) (#1837).
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest


class TestSensorPyImportsOffTheEventLoop(unittest.TestCase):
    def test_sensor_py_imports_solver_writer_via_the_import_executor(self):
        sensor_py = os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
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
        nimbus_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
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
