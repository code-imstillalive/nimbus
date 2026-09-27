"""`async_unregister_services()` must remove every service
`async_register_services()` registers (nimbus issue #1376).

## Why a derived test rather than a count

This gap has now existed three times, and each time the thing that let it
through was a number written down by hand:

- #365 item 1 built `async_unregister_services()` because a hub removal left
  `solve_now`/`retrain`/`compute_quality_report` registered and callable
  forever, with `solver_runtime.async_run_solve()` still bound through a stale
  `set_native_hass()` -- a solve with no entities.
- #809's `set_controllable_load` and #495's `flex_telemetry_record` were each
  added to the register side, and the docstring's "all four" went stale.
- #1376: `rescore_history` was registered but never unregistered, so that same
  #365 defect was live again for one service. PR #1368 *noticed* it, correctly
  declined to fix it in the same breath as unrelated work, and said "filed
  separately" -- and the follow-up was never filed.

A test asserting `async_remove.call_count == N` cannot catch any of that: it
only pins how many services are torn down, never whether that equals how many
exist. `test_init_unload_resets_solver_runtime_globals` was raised 3 -> 4 -> 5
across those changes and passed every time, because 5 genuinely was the number
being removed. It was never the number registered.

So this derives both sets from `services.py`'s own source and compares them.
Add a service and forget the teardown, and the set difference names it.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import _solver_path  # noqa: F401 -- sys.path setup side effect
import solver_writer

_SERVICES_PY = (
    pathlib.Path(solver_writer.__file__.replace(".pyc", ".py")).parent / "services.py"
)


def _service_constants_in(func_name: str) -> set[str]:
    """Every `SERVICE_*` name referenced inside one top-level function.

    Reads the AST rather than the text so a name inside a comment or a
    docstring -- and #1376's own explanatory comment mentions several -- cannot
    be mistaken for a real reference.
    """
    tree = ast.parse(
        _SERVICES_PY.read_text(encoding="utf-8"), filename=str(_SERVICES_PY)
    )
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == func_name
        ),
        None,
    )
    assert fn is not None, f"{func_name} not found in {_SERVICES_PY.name}"
    return {
        n.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Name)
        and n.id.startswith("SERVICE_")
        and not n.id.endswith("_SCHEMA")
    }


class TestServicesRegisterAndUnregisterAreSymmetric(unittest.TestCase):
    def test_every_registered_service_is_also_unregistered(self):
        registered = _service_constants_in("async_register_services")
        unregistered = _service_constants_in("async_unregister_services")
        missing = sorted(registered - unregistered)
        self.assertEqual(
            missing,
            [],
            f"{missing} are registered by async_register_services() but never "
            f"removed by async_unregister_services(). A hub removal therefore "
            f"leaves them callable forever, with solver_runtime still bound "
            f"through a stale set_native_hass() -- the exact #365 item-1 "
            f"defect async_unregister_services() exists to prevent. Add them "
            f"to its tuple.",
        )

    def test_nothing_is_unregistered_that_was_never_registered(self):
        """The other direction, which would be a different bug: removing a
        service this module does not own."""
        registered = _service_constants_in("async_register_services")
        unregistered = _service_constants_in("async_unregister_services")
        extra = sorted(unregistered - registered)
        self.assertEqual(
            extra,
            [],
            f"{extra} are removed by async_unregister_services() but never "
            f"registered by async_register_services(). Either the name is "
            f"stale, or a service is being registered somewhere this test "
            f"does not look.",
        )

    def test_the_sweep_is_not_vacuous(self):
        """Guards the guard: an AST walk that silently matched nothing would
        make both assertions above pass over empty sets."""
        registered = _service_constants_in("async_register_services")
        self.assertGreaterEqual(
            len(registered),
            6,
            f"expected at least the six known services, found {sorted(registered)} "
            f"-- if the registration moved out of async_register_services(), this "
            f"test needs to follow it rather than quietly passing.",
        )


if __name__ == "__main__":
    unittest.main()
