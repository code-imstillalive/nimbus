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

_SOLVER_PARENT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "nimbus_load",
)
if _SOLVER_PARENT not in sys.path:
    # APPENDED, not inserted at position 0. This directory contains the
    # integration's own HA platform modules, one of which is `select.py`
    # -- and `select` is a STDLIB module. Putting this directory first
    # makes `import select` resolve to the HA platform, so the next
    # `import socket` (which imports `selectors`, which imports `select`)
    # fails with a partially-initialised-module ImportError, taking every
    # later `asyncio`/`homeassistant` import down with it. Appending lets
    # stdlib win while still making `from solver import ...` resolve,
    # because nothing else on the path provides `solver`.
    sys.path.append(_SOLVER_PARENT)
