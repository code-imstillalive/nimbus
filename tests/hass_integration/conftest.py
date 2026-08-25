"""Pytest configuration for the `hass_integration` test subdir.

These tests use the REAL Home Assistant test harness
(`pytest-homeassistant-custom-component`) instead of the stub tree
under `tests/_ha_stubs.py`. They live in their own subdirectory AND
run as a separate pytest invocation from the stub-based tests -- see
tests/hass_integration/README.md for why both together are needed for
real isolation.

The six things this conftest does:

  1. Autouse `enable_custom_integrations` so every test here gets the
     custom_components/ path treatment without asking for the fixture
     by name.
  2. Autouse `recorder_mock`, requested BEFORE anything touches
     `hass`, so the recorder actually exists. Nimbus declares
     `"dependencies": ["recorder"]` in its manifest, so without it
     `async_setup_entry` fails with `KeyError: 'recorder'` from
     recorder/core.py and every test here fails before it reaches the
     behaviour it is testing.
  3. Autouse setup of the `http` and `frontend` components. Nimbus
     also declares `"http"` as a dependency as of v0.74.0 (the
     bundled switchboard-topology-card frontend registration), so
     HA refuses to set the entry up at all without it -- same class
     of trap as #2, same shape of fix.
  4. Redirect the solver's on-disk state files into `tmp_path`, so a
     real solve does not try to write to `/opt/`.
  5. Bind this test's `hass` into `solver_writer` and unbind it after,
     so the entity dispatch seam is live and nothing leaks between
     tests.
  6. Set pytest-asyncio's `asyncio_mode = "auto"` so `async def
     test_...` functions are awaited automatically, matching pytest-
     homeassistant-custom-component's own convention.

Both are directory-local -- setting `asyncio_mode` in the top-level
`pyproject.toml` would flip the mode for the stub tests too (they run
in a separate pytest invocation, but a globally-set option would still
be picked up by both), so scoping it here keeps the boundary clean.

Note on Python versions: 3.14.0 through 3.14.3 cannot run this suite.
`recorder_mock` patches recorder functions with `autospec=True`, and on
those releases `unittest.mock` inspected signatures in
`annotationlib.Format.VALUE` mode, which eagerly evaluates annotations
naming `TYPE_CHECKING`-only types and raises a bare `NameError` from
inside recorder. CPython fixed it by passing `Format.FORWARDREF`
(`Lib/unittest/mock.py`, `_get_signature_object`); 3.14.3 does not have
that call and 3.14.4 does. Hence the `>=3.14.4` floor in
`pyproject.toml` rather than a workaround in here.
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
        ("SOLAR_DELIVERY_RATIO_PATH", "nimbus_solver_solar_delivery_ratio.json"),
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


@pytest.fixture(autouse=True)
async def http_and_frontend_set_up(hass):
    """Set up `http` and `frontend` components before Nimbus's entry setup.

    Nimbus declares `"http"` as a manifest dependency as of v0.74.0
    (needed by `frontend.async_register_frontend()` to serve the
    bundled `switchboard-topology-card.js` via
    `hass.http.async_register_static_paths`, and to register the
    module URL with the Lovelace frontend via
    `frontend.add_extra_js_url()`). Without this fixture the test
    harness starts `hass` without the `http` component wired up,
    so `hass.config_entries.async_setup(entry.entry_id)` never
    completes -- HA refuses to set the entry up at all -- and every
    test here fails with 0 live entity instances rather than
    reaching the behaviour it is trying to test.

    `frontend` is set up alongside because `add_extra_js_url` reads
    and mutates `hass.data[DATA_EXTRA_MODULE_URL]`, which
    `frontend`'s own setup initialises. The `try/except Exception`
    guard around the registration in `__init__.py` would swallow a
    genuine failure here without any visible symptom, so bringing
    the component up matches the production path.

    Kept as a directory-local, autouse fixture (rather than a
    top-level pytest plugin) so it stays scoped to the tests that
    genuinely need it and doesn't accidentally leak into the
    stub-based suite.
    """
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "frontend", {})
    yield
