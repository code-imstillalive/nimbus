"""Shared import shim so test modules in this directory can `from ml import
model` exactly like the real integration does, without ever executing the
real custom_components/nimbus_load/__init__.py (which imports
homeassistant.* at module level and would crash outside a real HA install).

Unlike solver/ (a genuinely self-contained package -- see _solver_path.py),
ml/model.py reaches OUTSIDE its own package with `from ..const import
LAG_LONG_STEPS, ...` -- a real relative import up to nimbus_load/const.py.
Plain sys.path injection of nimbus_load/ itself (making `ml` a top-level
package) breaks that `..` -- there'd be no parent package to go up to.

Fix: pre-register a bare, empty `nimbus_load` module in sys.modules whose
`__path__` points at the real directory, BEFORE anything imports `ml`.
Python only ever executes a package's __init__.py on its first real import;
since this stub is already sitting in sys.modules by then, `import
nimbus_load.ml.model` finds `ml` as a subpackage under that path and skips
straight to it, while `from ..const import X` inside ml/model.py resolves
against this same stub's __path__ and imports the REAL const.py fresh
(confirmed HA-import-free, see const.py itself) -- getting the exact real
production code under test, not a reimplementation, with zero HA
dependency.
"""

import os
import sys
import types

_NIMBUS_LOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "nimbus_load",
)

if "nimbus_load" not in sys.modules:
    _stub = types.ModuleType("nimbus_load")
    _stub.__path__ = [_NIMBUS_LOAD_DIR]
    sys.modules["nimbus_load"] = _stub
