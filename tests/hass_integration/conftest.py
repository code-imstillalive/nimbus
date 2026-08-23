"""Pytest configuration for the `hass_integration` test subdir.

These tests use the REAL Home Assistant test harness
(`pytest-homeassistant-custom-component`) instead of the stub tree
under `tests/_ha_stubs.py`. They live in their own subdirectory AND
run as a separate pytest invocation from the stub-based tests -- see
tests/hass_integration/README.md for why both together are needed for
real isolation.

The two things this conftest does:

  1. Autouse `enable_custom_integrations` so every test here gets the
     custom_components/ path treatment without asking for the fixture
     by name.
  2. Set pytest-asyncio's `asyncio_mode = "auto"` so `async def
     test_...` functions are awaited automatically, matching pytest-
     homeassistant-custom-component's own convention.

Both are directory-local -- setting `asyncio_mode` in the top-level
`pyproject.toml` would flip the mode for the stub tests too (they run
in a separate pytest invocation, but a globally-set option would still
be picked up by both), so scoping it here keeps the boundary clean.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    """Set pytest-asyncio's mode for this invocation only. When CI (or
    a dev) runs `pytest tests/hass_integration/`, this sets mode=auto
    so plain `async def test_...` functions work without decorators.
    The stub-based suite runs as a SEPARATE pytest invocation and
    isn't affected -- see .github/workflows/ci.yml for the two-step
    layout.
    """
    config.option.asyncio_mode = "auto"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Every test in this subdir needs `custom_components/` on the
    import path so the real HA harness can load Nimbus as if it were
    a HACS-installed integration. See pytest-homeassistant-custom-
    component's docs for what the fixture does -- in short, it flips
    `hass.config_entries` into treating custom_components as
    first-class."""
    yield
