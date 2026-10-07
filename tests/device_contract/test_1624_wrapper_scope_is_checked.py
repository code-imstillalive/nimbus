"""nimbus #1624 (IV&V pass, following #1604's DC-R15 inventory scanner):
the pass-through exclusion for a wrapper's own `hass.services.async_call(domain_var, ...)`
call site used to fire whenever ANY wrapper existed anywhere in the codebase, not only
when the call site was actually lexically inside a wrapper function.

Reproduced before the fix: a function outside `solver_dispatch/` that assigns a domain
to a local variable before calling `hass.services.async_call` was invisible to the
scanner -- not even recorded as "<dynamic>" -- as long as some unrelated wrapper existed
anywhere in the package. That is exactly the "guard that cannot fail" shape this repo's
own CLAUDE.md documents repeatedly (#357, #594, #952, #955): DC-R15 exists specifically
to catch a dynamic-domain service call outside `solver_dispatch/`, and the one call shape
most likely to be dynamic (a variable passed as the domain) was the one shape it couldn't
see.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "_dc_baseline_1624", ROOT / "scripts" / "device_contract_baseline.py"
)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)

_WRAPPER_SRC = '''\
async def ha_call_service_with_response(hass, domain, service, data):
    """A real wrapper: its own call site carries the real (dynamic) domain."""
    return await hass.services.async_call(domain, service, data)
'''

_EVIL_SRC = '''\
async def do_bad_thing(hass):
    """Not a wrapper -- just an ordinary function that happens to build its
    domain from a local variable before calling the service, the same shape
    DC-R15 exists to catch outside solver_dispatch/."""
    the_domain = "switch"
    await hass.services.async_call(the_domain, "turn_on", {"entity_id": "switch.x"})
'''


def _make_pkg(tmp_path: Path, *, with_evil: bool) -> Path:
    pkg = tmp_path / "custom_components" / "nimbus_load"
    pkg.mkdir(parents=True)
    (pkg / "const.py").write_text("", encoding="utf-8")
    (pkg / "solver_writer.py").write_text(_WRAPPER_SRC, encoding="utf-8")
    if with_evil:
        (pkg / "evil_actuator.py").write_text(_EVIL_SRC, encoding="utf-8")
    return tmp_path


def test_a_wrappers_own_call_site_is_still_excluded(tmp_path: Path) -> None:
    """The fix must not regress the thing the exclusion exists for: a real
    wrapper's own pass-through call site is still skipped from `service_calls`."""
    root = _make_pkg(tmp_path, with_evil=False)
    _, _, services, _ = baseline.scan_python(root, keys={})
    assert services == []


def test_a_dynamic_domain_call_outside_a_wrapper_is_caught(tmp_path: Path) -> None:
    """The real finding: a non-wrapper function building its domain from a
    local variable, outside solver_dispatch/, must be recorded -- not
    silently dropped just because some unrelated wrapper exists in the tree."""
    root = _make_pkg(tmp_path, with_evil=True)
    _, _, services, _ = baseline.scan_python(root, keys={})
    evil = [s for s in services if s["module"] == "evil_actuator.py"]
    assert len(evil) == 1, services
    assert evil[0]["domain"] == "<dynamic>"
    assert evil[0]["control_capable"] is False  # domain unresolved, not asserted safe
