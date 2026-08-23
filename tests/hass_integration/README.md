# `tests/hass_integration/`

Tests here use the **real** Home Assistant test harness
(`pytest-homeassistant-custom-component`) instead of the stub tree
under [`tests/_ha_stubs.py`](../_ha_stubs.py). They exist for one
specific reason: some regressions can only be observed by driving the
actual HA state machine, event loop, and config-entry lifecycle.

## When to put a test here vs. in the stub tree

Use `tests/hass_integration/` **only if the test needs one or more of**:

- The real HA state machine (`hass.states.async_set` -> visible state
  transitions).
- Real `async_track_time_interval` firing via `async_fire_time_changed`.
- A real config-entry lifecycle (`async_setup_entry`,
  `async_unload_entry`, `async_reload`), including entity teardown
  hooks (`async_will_remove_from_hass`).
- Instance identity across reloads (proving a fresh instance replaced
  an old one).

Everything else should stay in `tests/` and use `install_ha_stubs()`.
The stub tree is faster to collect and cheaper to run in CI, and 300+
existing tests already prove it.

## What's here

- [`test_flap_regression_state_stability.py`](test_flap_regression_state_stability.py)
  — The single invariant that would have caught the v0.73.0
  (#82) / v0.73.1 (#83) / v0.73.2 (#85) chain: after a real push, the
  entity's PUBLISHED state stays stable across the full recheck-timer
  cadence, not just at the instant of `update_from_solver()`.
- [`test_flap_regression_reload_instance.py`](test_flap_regression_reload_instance.py)
  — After a config-entry reload, exactly ONE live entity instance
  drives each well-known `entity_id`. Directly locks in the leading
  hypothesis for #85 (stale second instance publishing `unknown` every
  recheck tick).

## Cost

`pytest-homeassistant-custom-component` pulls in the full HA runtime
(~200 MB). It's opt-in per subdirectory via the autouse
`enable_custom_integrations` fixture in this directory's `conftest.py`,
so the stub-based tests in the parent directory pay no cost.
