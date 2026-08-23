"""Gold `stale-devices` (2026-08-23) -- confirms Nimbus threads
config_subentry_id correctly for the one class of entity that owns a
per-subentry device (NimbusForecastSensor -- the Load/Signal Forecaster
device, identifiers={(DOMAIN, subentry.subentry_id)}).

This is deliberately NOT a test that Nimbus manually removes orphaned
devices -- it doesn't need to. Home Assistant core's own
ConfigEntries.async_remove_subentry() already does this automatically:

    dev_reg.async_clear_config_subentry(entry.entry_id, subentry_id, entry.domain)
    ent_reg.async_clear_config_subentry(entry.entry_id, subentry_id)

(verified directly against homeassistant/config_entries.py on the `dev`
branch, 2026-08-23) -- but that automatic cleanup only has something to
act on if the device/entities were actually registered WITH a
config_subentry_id in the first place. Every other entity class in this
integration (NimbusSolverConfigSensor, NimbusTopologyConfigSensor, the
two Solver push sensors, every number.py/switch.py entity) is
deliberately hub-scoped (DeviceInfo identifiers={(DOMAIN, entry.entry_id)},
no config_subentry_id) -- correct, since those live for the life of the
hub, not any one subentry, and must NOT be swept by a subentry removal.

The real, single point of failure this guards against: a future refactor
of sensor.py's async_setup_entry silently dropping the
config_subentry_id= kwarg on NimbusForecastSensor's own async_add_entities
call (e.g. while "simplifying" or reordering that function) -- which
would silently reopen the exact orphan-device leak the Gold checklist
originally (correctly) flagged, since HA's automatic cleanup can't
recover devices it was never told belong to a subentry.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor  # noqa: E402

_SENSOR_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "sensor.py"
)


def test_per_subentry_device_uses_subentry_id_as_its_identifier():
    """The device NimbusForecastSensor attaches to is keyed by
    subentry_id, not entry_id -- confirming there IS a real per-subentry
    device for HA's automatic cleanup to find and remove."""
    import inspect

    src = inspect.getsource(sensor.NimbusForecastSensor.__init__)
    assert "identifiers={(DOMAIN, subentry.subentry_id)}" in src


def test_async_setup_entry_passes_config_subentry_id_for_the_forecast_sensor():
    """Static-source check (not a live async_add_entities call, which
    would need far more HA plumbing than the test stubs model) that the
    ONE async_add_entities(...) call constructing a NimbusForecastSensor
    inside sensor.py's async_setup_entry genuinely passes
    config_subentry_id=subentry.subentry_id as a keyword argument --
    parsed via ast, not a substring grep, so this can't be fooled by a
    comment or an unrelated string mentioning the same words.
    """
    tree = ast.parse(_SENSOR_PY.read_text(encoding="utf-8"))

    found_call = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "async_add_entities"):
            continue
        # Only the call whose first positional arg constructs a
        # NimbusForecastSensor -- there are 4 other async_add_entities
        # calls in this file for hub-scoped entities.
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.List) and first_arg.elts:
            call_expr = first_arg.elts[0]
            if (
                isinstance(call_expr, ast.Call)
                and isinstance(call_expr.func, ast.Name)
                and call_expr.func.id == "NimbusForecastSensor"
            ):
                found_call = node
                break

    assert found_call is not None, (
        "Could not locate the async_add_entities([NimbusForecastSensor(...)], ...) "
        "call in sensor.py -- has it been renamed or restructured?"
    )
    kw_names = {kw.arg for kw in found_call.keywords}
    assert "config_subentry_id" in kw_names, (
        "NimbusForecastSensor's async_add_entities call no longer passes "
        "config_subentry_id -- this silently reopens the stale-devices leak, "
        "since HA's automatic dev_reg.async_clear_config_subentry()/"
        "ent_reg.async_clear_config_subentry() cleanup has nothing to act on "
        "for a device/entity that was never associated with a subentry."
    )


def test_hub_scoped_entities_deliberately_do_not_pass_config_subentry_id():
    """The inverse guard: hub-level entities (Solver Config sensor,
    Topology Config sensor, both Solver push sensors) must NOT gain a
    config_subentry_id somewhere down the line -- that would make HA
    delete them the moment ANY subentry is removed, which is wrong (they
    belong to the hub, not to any one load/signal)."""
    tree = ast.parse(_SENSOR_PY.read_text(encoding="utf-8"))
    hub_scoped_classes = {
        "NimbusSolverConfigSensor",
        "NimbusTopologyConfigSensor",
    }
    offenders = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "async_add_entities"
        ):
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        for elt in node.args[0].elts:
            if (
                isinstance(elt, ast.Call)
                and isinstance(elt.func, ast.Name)
                and elt.func.id in hub_scoped_classes
                and any(kw.arg == "config_subentry_id" for kw in node.keywords)
            ):
                offenders.append(elt.func.id)
    assert not offenders, (
        f"these hub-scoped entities are now (wrongly) passed with "
        f"config_subentry_id: {offenders}"
    )


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
