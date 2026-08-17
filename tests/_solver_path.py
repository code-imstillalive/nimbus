"""Shared sys.path setup so every test module in this directory can
`from solver import elements, network` exactly like the real writer
scripts (sibling 116KAT-HA-AI repo) do -- this package is not pip-
installed, it's a direct HA custom_component clone, so path setup is
needed the same way every real deploy already needs it.

No real test framework (pytest) is assumed available on every machine
this might run on (this project's own established finding: "no working
local Python interpreter was available to test with directly" has hit
more than once) -- every test module here uses stdlib `unittest` only,
runnable via `python -m unittest discover tests` or directly via
`python tests/test_X.py` with zero extra dependencies.
"""
import os
import sys

_SOLVER_PARENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "nimbus_load")
if _SOLVER_PARENT not in sys.path:
    sys.path.insert(0, _SOLVER_PARENT)
