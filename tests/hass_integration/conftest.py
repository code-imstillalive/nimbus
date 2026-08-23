"""Pytest configuration for the `hass_integration` test subdir.

These tests use the REAL Home Assistant test harness
(`pytest-homeassistant-custom-component`) instead of the stub tree
under `tests/_ha_stubs.py`. They live in their own subdirectory for
two reasons:

  1. Isolation. The stub-based tests deliberately don't import HA
     itself -- installing HA into `sys.modules` at collection time
     (which pytest-homeassistant-custom-component does) would clobber
     those stubs and break the 313 existing tests. Keeping the two
     test styles in separate directories prevents any cross-
     contamination via pytest's collection-time imports.

  2. Opt-in cost. `pytest-homeassistant-custom-component` pulls in the
     full HA runtime (~200 MB, ~5s collection overhead). The stub
     tests stay fast; only these files pay the cost, and only when
     specifically requested (`pytest tests/hass_integration/` or the
     full-suite CI job).

The `enable_custom_integrations` and `auto_enable_custom_integrations`
fixtures come from `pytest-homeassistant-custom-component`. The
auto-enable version fires for every test in this directory without
each test having to request the fixture by name.
"""

from __future__ import annotations

import pytest

# All tests in this subdirectory are `async def` and use the real HA
# event loop from the `hass` fixture. `asyncio_mode = "auto"` scoped
# here (not globally in pyproject.toml) means pytest-asyncio auto-
# awaits them without every test needing `@pytest.mark.asyncio`, and
# leaves the (non-async) tests in the sibling directory untouched.
collect_ignore_glob: list[str] = []


def pytest_collection_modifyitems(config, items):
    """Force asyncio mode for every test in this subdir. Same effect
    as `pytest.ini`'s `asyncio_mode = auto`, but scoped -- doesn't
    change the marker mode for the sibling stub-based tests."""
    import pytest_asyncio  # noqa: F401 -- import proves the plugin is installed

    for item in items:
        if "hass_integration" in str(item.fspath):
            if not any(m.name == "asyncio" for m in item.iter_markers()):
                item.add_marker(pytest.mark.asyncio)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Every test in this subdir needs `custom_components/` on the
    import path so the real HA harness can load Nimbus as if it were
    a HACS-installed integration. See pytest-homeassistant-custom-
    component's docs for what the fixture does -- in short, it flips
    `hass.config_entries` into treating custom_components as
    first-class."""
    yield
