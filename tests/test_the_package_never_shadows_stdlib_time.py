"""Loading the `time` platform sets the package's attribute `time` to
time.py, which replaced a stdlib `import time` in __init__.py, and setup then
failed on `time.monotonic()` (11 Oct 2026). A check that reads source text;
the time entities themselves are in tests/v2/entities/test_switches_and_times.py
(#1837)."""

from __future__ import annotations

from datetime import time
from pathlib import Path

from tests._ha_stubs import install_ha_stubs

install_ha_stubs()

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _helpers():
    src = (_PKG / "time.py").read_text(encoding="utf-8")
    start = src.index("def hours_to_time")
    end = src.index("async def async_setup_entry")
    ns: dict = {"time": time}
    exec(src[start:end], ns)  # noqa: S102 -- the pure helpers only, without HA imports
    return ns


def test_the_package_never_uses_the_name_time_for_the_stdlib() -> None:
    """Loading the `time` platform sets the package's attribute `time` to
    time.py, replacing a stdlib `import time` in __init__.py: setup then
    failed on `time.monotonic()` (devhub, 11 Oct 2026)."""
    src = (_PKG / "__init__.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "\nimport time\n" not in "\n" + code + "\n"
    assert "time.monotonic(" not in code
    assert "time.time(" not in code
