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

# nimbus issue #1329: the REPO ROOT, so `from tests.test_x import y` resolves.
#
# A test module that imports a sibling test module by BARE name gets a second
# module object for the same file -- pytest imports it as
# `tests.test_x` and the bare import creates `test_x`, both in sys.modules. When
# that file installs anything global at module level (the real case: a
# `_FakeRunStateStore` written into `sys.modules["homeassistant.helpers.storage"]`)
# the second import re-runs it, the last import wins, and the class the tests
# clear in `setUp` is no longer the class that is installed. Measured: 35
# failures, and only in one collection order (Mark Purcell, #1329 -- he
# root-caused it; reproduced here, 35 failed / 90 passed against 125 passed
# with the sibling imports package-qualified).
#
# Qualifying those imports is the fix, and it needs the repo root importable.
# Under pytest `pythonpath` supplies `tests` and the integration dir but NOT the
# root, and standalone `python tests/test_x.py` supplies only `tests` -- so
# without this, qualifying the imports would break the standalone mode this
# module's own docstring promises. Verified both ways.
#
# APPENDED for the same reason as below: nothing here should be allowed to
# shadow a stdlib name. (Checked: the repo root holds no top-level .py files.)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

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
