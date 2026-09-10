"""number.py's own sibling of test_stale_devices_cleanup.py -- see that
file's own docstring for the full config_subentry_id background and the
real #680 incident this guards against.

nimbus issue #680 (Mark Purcell, real live regression, found within 24h
of #645 shipping): number.py's own async_setup_entry registered every
NimbusControllableLoadNumber entity via ONE async_add_entities() call
covering every controllable_load subentry at once, with no
config_subentry_id at all. sensor.py's own commanded_state family
attaches to the SAME per-load device (DeviceInfo identifiers={(DOMAIN,
subentry.subentry_id)}) WITH config_subentry_id=subentry.subentry_id --
HA's own core warning ("assigns an existing device to a different
config subentry... this silently moves the device") fired exactly
once, during the restart that loaded #645, and sensor.py's own entity
family for that device vanished from the registry as a live
consequence. Fixed by looping per-subentry and passing
config_subentry_id=subentry.subentry_id on each call, matching
sensor.py's own established, correct pattern for the same device.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import number

_NUMBER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "number.py"
)


def test_per_subentry_device_uses_subentry_id_as_its_identifier():
    """Same real per-subentry device NimbusForecastSensor/the commanded_
    state family attach to -- confirming there IS a real per-subentry
    device here too, for HA's automatic cleanup to find and remove."""
    import inspect

    src = inspect.getsource(number.NimbusControllableLoadNumber.__init__)
    assert "identifiers={(DOMAIN, subentry.subentry_id)}" in src


def test_async_setup_entry_passes_config_subentry_id_for_controllable_load_numbers():
    """Static-source check (ast, not a substring grep) that the
    async_add_entities(...) call constructing NimbusControllableLoadNumber
    entities inside number.py's async_setup_entry genuinely passes
    config_subentry_id=subentry.subentry_id as a keyword argument --
    the exact thing #680 found missing."""
    tree = ast.parse(_NUMBER_PY.read_text(encoding="utf-8"))

    # Variable names assigned from a list comprehension whose own element
    # expression constructs NimbusControllableLoadNumber(...) -- supports
    # this file's own "build the list, then hand it to async_add_entities"
    # shape (as opposed to sensor.py's inline list literal).
    controllable_load_list_vars = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.ListComp)):
            continue
        elt = node.value.elt
        if (
            isinstance(elt, ast.Call)
            and isinstance(elt.func, ast.Name)
            and elt.func.id == "NimbusControllableLoadNumber"
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    controllable_load_list_vars.add(target.id)

    assert controllable_load_list_vars, (
        "Could not find a list-comprehension variable built from "
        "NimbusControllableLoadNumber(...) in number.py -- has the "
        "construction shape changed?"
    )

    found_call = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "async_add_entities"):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if (
            isinstance(first_arg, ast.Name)
            and first_arg.id in controllable_load_list_vars
        ):
            found_call = node
            break

    assert found_call is not None, (
        "Could not locate the async_add_entities(<controllable-load-numbers>, ...) "
        "call in number.py -- has it been renamed or restructured?"
    )
    kw_names = {kw.arg for kw in found_call.keywords}
    assert "config_subentry_id" in kw_names, (
        "NimbusControllableLoadNumber's async_add_entities call does not pass "
        "config_subentry_id -- this is exactly nimbus issue #680: HA silently "
        "reassigns the shared per-load device away from its subentry, and "
        "sensor.py's own commanded_state family for that same device vanishes "
        "from the entity registry as a live consequence."
    )


def test_hub_scoped_solver_numbers_deliberately_do_not_pass_config_subentry_id():
    """The inverse guard: NimbusSolverNumber (the hub-level Solver
    settings) must NOT gain a config_subentry_id -- it belongs to the
    hub, not to any one load, and must survive every subentry removal."""
    tree = ast.parse(_NUMBER_PY.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "async_add_entities"
        ):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        constructs_solver_number = False
        if isinstance(first_arg, ast.List):
            constructs_solver_number = any(
                isinstance(elt, ast.Call)
                and isinstance(elt.func, ast.Name)
                and elt.func.id == "NimbusSolverNumber"
                for elt in first_arg.elts
            )
        elif isinstance(first_arg, ast.ListComp):
            elt = first_arg.elt
            constructs_solver_number = (
                isinstance(elt, ast.Call)
                and isinstance(elt.func, ast.Name)
                and elt.func.id == "NimbusSolverNumber"
            )
        if constructs_solver_number and any(
            kw.arg == "config_subentry_id" for kw in node.keywords
        ):
            offenders.append("NimbusSolverNumber")
    assert not offenders, (
        f"these hub-scoped entities are now (wrongly) passed with "
        f"config_subentry_id: {offenders}"
    )


if __name__ == "__main__":
    import unittest

    unittest.main()
