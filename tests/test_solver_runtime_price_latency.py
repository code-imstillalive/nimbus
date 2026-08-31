"""Real test of solver_runtime.py's new price-response-latency registry
(issues #294/#295, Mark Purcell, 2026-08-31) -- record_solve_completed(),
time_since_last_solve(), and register_price_latency_sensor().

Deliberately does NOT go through __init__.py's own cron/price-watcher
wiring (see tests/test_price_watcher.py and tests/test_init_cron_
suppression.py for that) -- this file is scoped exactly to the module-
level registry functions themselves: does a "cron"/"startup" trigger
update the elapsed-time tracker without touching the sensor, does a
"price_change" trigger compute the right latency and forward the right
attributes to the sensor, and does time_since_last_solve() correctly
report None before anything has run.

Same testing convention as tests/test_price_watcher.py: bare pytest-
style functions, tests/_ha_stubs.py's stand-in homeassistant.* modules
installed first so custom_components.nimbus_load.solver_runtime imports
resolve at all, and explicit module-level state resets at the start of
every test (this project's own established alternative to a fixture-
based reset -- see test_price_watcher.py's own _reset_module_state()
docstring for why: a stale value from a previous test's own call would
otherwise silently leak into the next one, since these are real module-
level globals, not per-test instances).
"""

import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime


def _reset_module_state() -> None:
    """Every test starts from a clean module-level state -- see this
    file's own module docstring for why a fixture isn't used here,
    matching test_price_watcher.py's own established convention."""
    solver_runtime._last_solve_completed_monotonic = None
    solver_runtime._price_latency_sensor = None


def test_time_since_last_solve_returns_none_before_any_solve():
    _reset_module_state()
    assert solver_runtime.time_since_last_solve() is None


def test_record_solve_completed_cron_updates_elapsed_time_but_not_sensor():
    _reset_module_state()
    fake_sensor = MagicMock()
    solver_runtime.register_price_latency_sensor(fake_sensor)

    solver_runtime.record_solve_completed(trigger_source="cron")

    since = solver_runtime.time_since_last_solve()
    assert since is not None and since >= 0.0, (
        "a completed cron solve must set _last_solve_completed_monotonic "
        "so issue #295's suppression check has something real to compare "
        "against"
    )
    # Issue #294's own explicit design: "For trigger_source=cron, last_
    # price_change_at is null and latency is null... the sensor sits at
    # its last event-driven value" -- a cron trigger must never touch
    # the latency sensor at all.
    fake_sensor.record.assert_not_called()


def test_record_solve_completed_startup_also_leaves_the_sensor_untouched():
    _reset_module_state()
    fake_sensor = MagicMock()
    solver_runtime.register_price_latency_sensor(fake_sensor)

    solver_runtime.record_solve_completed(trigger_source="startup")

    assert solver_runtime.time_since_last_solve() is not None
    fake_sensor.record.assert_not_called()


def test_time_since_last_solve_reflects_real_elapsed_time():
    # A generous sleep + a loose lower bound -- this is checking "does
    # this reflect real elapsed time at all" (catches a stale/zeroed
    # implementation), not measuring precise timer resolution, so a wide
    # margin avoids OS scheduler jitter flaking the test.
    _reset_module_state()
    solver_runtime.record_solve_completed(trigger_source="cron")
    time.sleep(0.15)
    since = solver_runtime.time_since_last_solve()
    assert since >= 0.10, (
        f"expected at least 0.10s elapsed after a 0.15s sleep, got "
        f"{since!r} -- time_since_last_solve() should reflect real wall/"
        "monotonic time, not a stale or zeroed value"
    )


def test_record_solve_completed_price_change_computes_latency_and_pushes_to_sensor():
    _reset_module_state()
    fake_sensor = MagicMock()
    solver_runtime.register_price_latency_sensor(fake_sensor)

    price_change_at = datetime(2026, 8, 31, 7, 55, 20, tzinfo=UTC)
    solve_at = datetime(2026, 8, 31, 7, 55, 26, 576000, tzinfo=UTC)

    with patch.object(solver_runtime.dt_util, "utcnow", return_value=solve_at):
        solver_runtime.record_solve_completed(
            trigger_source="price_change",
            triggering_entity="sensor.amber_express_amber_feed_in_price",
            price_change_at=price_change_at,
            debounce_s=5.0,
        )

    fake_sensor.record.assert_called_once()
    kwargs = fake_sensor.record.call_args.kwargs
    assert kwargs["trigger_source"] == "price_change"
    assert kwargs["triggering_entity"] == "sensor.amber_express_amber_feed_in_price"
    assert kwargs["price_change_at"] == price_change_at
    assert kwargs["solve_at"] == solve_at
    assert kwargs["debounce_s"] == 5.0
    # 07:55:26.576 - 07:55:20.000 = 6.576s -- matches Mark's own real
    # #232 verification table's shape exactly (a genuine, single-digit-
    # second latency, not a stale/zeroed/None value).
    assert abs(kwargs["latency_s"] - 6.576) < 1e-6


def test_record_solve_completed_price_change_with_no_price_change_at_gives_null_latency():
    """Mark's own #294 proposal covers this explicitly: a price_change
    trigger CAN in principle fire with no known trigger timestamp (e.g. a
    future solve_now-style call re-using this same trigger_source without
    a real originating event) -- latency should come back None, not raise
    or silently compute nonsense against `None`.
    """
    _reset_module_state()
    fake_sensor = MagicMock()
    solver_runtime.register_price_latency_sensor(fake_sensor)

    with patch.object(
        solver_runtime.dt_util,
        "utcnow",
        return_value=datetime(2026, 8, 31, 8, 0, 0, tzinfo=UTC),
    ):
        solver_runtime.record_solve_completed(
            trigger_source="price_change",
            triggering_entity="sensor.some_price",
            price_change_at=None,
            debounce_s=5.0,
        )

    kwargs = fake_sensor.record.call_args.kwargs
    assert kwargs["latency_s"] is None
    assert kwargs["price_change_at"] is None


def test_record_solve_completed_price_change_with_no_sensor_registered_is_a_noop():
    _reset_module_state()
    # No register_price_latency_sensor() call -- must not raise even
    # though a real price_change trigger fired.
    solver_runtime.record_solve_completed(
        trigger_source="price_change",
        triggering_entity="sensor.some_price",
        price_change_at=datetime(2026, 8, 31, 8, 0, 0, tzinfo=UTC),
        debounce_s=5.0,
    )
    assert solver_runtime.time_since_last_solve() is not None


def test_unrecognised_trigger_source_is_treated_like_cron_not_price_change():
    """record_solve_completed()'s own docstring: an unrecognised
    trigger_source string just means the sensor update is skipped --
    this locks that in rather than leaving it as an unverified claim."""
    _reset_module_state()
    fake_sensor = MagicMock()
    solver_runtime.register_price_latency_sensor(fake_sensor)

    solver_runtime.record_solve_completed(trigger_source="something_new")

    fake_sensor.record.assert_not_called()
    assert solver_runtime.time_since_last_solve() is not None


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
