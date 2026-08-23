"""Pytest configuration for the `hass_integration` test subdir.

These tests use the REAL Home Assistant test harness
(`pytest-homeassistant-custom-component`) instead of the stub tree
under `tests/_ha_stubs.py`. They live in their own subdirectory AND
run as a separate pytest invocation from the stub-based tests -- see
tests/hass_integration/README.md for why both together are needed for
real isolation.

The five things this conftest does:

  1. Autouse `enable_custom_integrations` so every test here gets the
     custom_components/ path treatment without asking for the fixture
     by name.
  2. Autouse `recorder_mock`, requested BEFORE anything touches
     `hass`, so the recorder actually exists. Nimbus declares
     `"dependencies": ["recorder"]` in its manifest, so without it
     `async_setup_entry` fails with `KeyError: 'recorder'` from
     recorder/core.py and every test here fails before it reaches the
     behaviour it is testing.
  3. Redirect the solver's on-disk state files into `tmp_path`, so a
     real solve does not try to write to `/opt/`.
  4. Bind this test's `hass` into `solver_writer` and unbind it after,
     so the entity dispatch seam is live and nothing leaks between
     tests.
  5. Set pytest-asyncio's `asyncio_mode = "auto"` so `async def
     test_...` functions are awaited automatically, matching pytest-
     homeassistant-custom-component's own convention.

Both are directory-local -- setting `asyncio_mode` in the top-level
`pyproject.toml` would flip the mode for the stub tests too (they run
in a separate pytest invocation, but a globally-set option would still
be picked up by both), so scoping it here keeps the boundary clean.
"""

from __future__ import annotations

import sys
import types
import unittest.mock

import pytest


class _ModuleShim(types.SimpleNamespace):
    """Stand in for a module with a small number of attributes replaced.

    Used instead of assigning to `inspect.signature` directly so the
    override is confined to `unittest.mock` and does not change how the
    rest of the process, including pytest itself, inspects signatures.
    """

    def __init__(self, module, **overrides):
        super().__init__(**overrides)
        self._module = module

    def __getattr__(self, name):
        return getattr(self._module, name)


# ---------------------------------------------------------------------------
# Python 3.14 / PEP 649 shim, needed before `recorder_mock` can run.
#
# Home Assistant's recorder code annotates functions with names imported
# only under `TYPE_CHECKING`, so those names do not exist at runtime.
# Under PEP 649 that is fine, because annotations are lazy, until
# something inspects them in `Format.VALUE` mode, which evaluates
# eagerly. `recorder_mock` patches several recorder functions with
# `autospec=True`, and `create_autospec` routes through
# `unittest.mock._get_signature_object` -> `inspect.signature`, whose
# `annotation_format` still defaults to `Format.VALUE`. Every test in
# this directory then errors at setup with a bare `NameError` naming a
# type, for example `Recorder` from `recorder/migration.py:319` or
# `Session` from `helpers/recorder.py:75`.
#
# Patching each name individually is whack-a-mole: the two above were
# found one after the other, and there is no reason to think they are
# the last. `mock` does not use the annotations for anything, it only
# wants the parameter list, so asking for `Format.FORWARDREF` gives it
# exactly what it needs and leaves unresolvable names as `ForwardRef`
# placeholders instead of raising. This is the same fix Django applied
# for the same pattern (https://code.djangoproject.com/ticket/36903).
#
# Scoped to `unittest.mock` on purpose, and only on 3.14+. Verified
# against homeassistant 2026.8.3 on CPython 3.14.3. This is an upstream
# defect rather than a Nimbus one: it hits any custom integration that
# declares recorder as a manifest dependency and uses `recorder_mock` on
# 3.14. Remove once pytest-homeassistant-custom-component or core fixes
# it upstream.
if sys.version_info >= (3, 14):
    import annotationlib

    _mock_signature = unittest.mock.inspect.signature

    def _signature_allowing_forward_refs(obj, *args, **kwargs):
        """Inspect a signature without evaluating its annotations."""
        kwargs.setdefault("annotation_format", annotationlib.Format.FORWARDREF)
        try:
            return _mock_signature(obj, *args, **kwargs)
        except TypeError:
            # Builtins and C functions reject the keyword.
            kwargs.pop("annotation_format", None)
            return _mock_signature(obj, *args, **kwargs)

    unittest.mock.inspect = _ModuleShim(  # type: ignore[assignment]
        unittest.mock.inspect, signature=_signature_allowing_forward_refs
    )


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
def solver_state_in_tmp_path(tmp_path, monkeypatch):
    """Redirect the solver's three on-disk state files into `tmp_path`.

    They default under `/opt/`, which is correct on a deployed add-on
    and unwritable everywhere else, so a real solve inside a test
    raises `PermissionError` from `acquire_lock()`.

    Setting the environment variables is not enough: each path is read
    from `os.environ` once at import time into a module-level constant,
    so by the time a fixture runs the value is already baked in. The
    constants themselves have to be patched. Doing it this way keeps
    the real file handling under test rather than stubbing it out, and
    gives each test its own directory so nothing leaks between them.
    """
    from custom_components.nimbus_load import solver_writer

    for name, filename in (
        ("LOCK_PATH", "nimbus_solver.lock"),
        ("PLAN_STATE_PATH", "nimbus_solver_last_plan.json"),
        ("LOAD_ERROR_NOTIFIED_PATH", "nimbus_solver_load_error.txt"),
    ):
        if hasattr(solver_writer, name):
            monkeypatch.setattr(solver_writer, name, str(tmp_path / filename))
    yield


@pytest.fixture(autouse=True)
def solver_writer_bound_to_hass(hass):
    """Bind this test's `hass` into `solver_writer`, and unbind after.

    `ha_post_state()` only routes through the entity dispatch seam when
    `solver_writer._NATIVE_HASS` is set. The one production caller of
    `set_native_hass()` is `solver_runtime.async_run_solve()`, which the
    `nimbus_entry` fixture deliberately patches out, so nothing binds
    `hass` and every push falls through to the REST fallback instead.
    That fallback opens a real socket, which `pytest_socket` blocks, so
    the test fails on a blocked-socket error that says nothing about
    what actually went wrong. Binding it here restores the seam the
    tests are meant to exercise without un-patching the solver.

    The teardown unbind is deliberate hygiene rather than a load-bearing
    fix, and mutation testing says so: deleting it leaves all nine tests
    passing, because setup rebinds before the next test runs. It stays
    because `_NATIVE_HASS` and `_ENTITY_UPDATE_HANDLERS` are
    module-level, which is right in production where one HA process
    imports the module once, but wrong for a test session that builds a
    fresh `hass` per test while the module stays imported. Any future
    test in this directory that pushes without going through this
    fixture would otherwise reach for a previous test's `hass` and hit
    `RuntimeError: Event loop is closed`, which is a genuinely confusing
    failure to debug. Cheap insurance against a real trap.
    """
    from custom_components.nimbus_load import solver_writer

    solver_writer.set_native_hass(hass)
    yield
    solver_writer.set_native_hass(None)
    solver_writer._ENTITY_UPDATE_HANDLERS.clear()


@pytest.fixture(autouse=True)
def auto_ha_environment(recorder_mock, enable_custom_integrations):
    """Stand up the recorder and the custom_components path, in that order.

    Order is load-bearing, which is why these are one fixture rather
    than two. `recorder_db_url` asserts `not hass_fixture_setup`, so
    the recorder has to be requested before anything touches `hass`.
    `enable_custom_integrations` does touch `hass`, so requesting it
    second in this signature is what keeps that assertion satisfied.

    Nimbus lists recorder as a hard manifest dependency, so HA refuses
    to set the entry up at all without it, and `custom_components/`
    has to be on the import path for the harness to load Nimbus as if
    it were HACS-installed.
    """
    yield
