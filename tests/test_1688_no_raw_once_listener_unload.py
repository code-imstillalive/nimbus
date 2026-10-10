"""nimbus #1688: no `entry.async_on_unload(hass.bus.async_listen_once(...))`.

A once-listener removes itself when its event fires, so registering its
unsub for unload calls it a second time on a later reload, and HA core logs
"Unable to remove unknown job listener" as an ERROR. `_run_once_started()`
in `__init__.py` is the safe form. The behaviour is tested on HA's real bus
in tests/hass_integration/test_1688_started_listener_unload.py; this is the
static guard that the pattern does not come back anywhere in the package.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"


def _raw_once_unloads() -> list[str]:
    found = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_on_unload"
            ):
                continue
            for arg in node.args:
                for inner in ast.walk(arg):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "async_listen_once"
                    ):
                        found.append(f"{path.relative_to(PKG)}:{node.lineno}")
    return found


def test_no_once_listener_unsub_is_registered_for_unload():
    assert not _raw_once_unloads(), (
        "a once-listener's unsub registered with async_on_unload is called "
        "twice after the event fires (#1688); use _run_once_started(): "
        + ", ".join(_raw_once_unloads())
    )


def test_both_startup_checks_use_the_safe_helper():
    src = (PKG / "__init__.py").read_text(encoding="utf-8")
    assert "_run_once_started(hass, entry, _pricing_check)" in src
    assert "_run_once_started(hass, entry, _energy_unit_check)" in src
