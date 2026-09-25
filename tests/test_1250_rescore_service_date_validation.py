"""IV&V finding (620f39b..4fd40c3 pass, 2026-09-26, head issue #1249, this
finding #1250): #1216 (c6f5b86, "Let the rescore service target one date")
added a real, new
code path to `_async_handle_rescore_history()` -- ISO-date parsing via
`date.fromisoformat()`, a server-side `back = (today - target).days`
derivation, and a `1 <= back <= 30` bounds check with two distinct
`ServiceValidationError` messages -- but shipped it with zero test
coverage of that path.

`tests/test_1208_rescore_button_costs_more_than_one_milp.py` (the same
commit's own pinning test) calls `solver_writer.rescore_quality_history()`
directly with a hand-built `only_dates` set; it never calls
`_async_handle_rescore_history()` itself, so the actual new logic --
the date parsing, the `back` arithmetic, and both validation-error
messages -- is exercised nowhere in the suite. A future edit to this
block (an off-by-one on the `1 <= back <= 30` boundary, a `date_str`
format that slips through the schema's own loose `Length(min=10, max=10)`
check but breaks `fromisoformat()` differently than expected, or a sign
flip in the `back` subtraction) would ship with nothing to catch it.

This test exercises `_async_handle_rescore_history()` itself end to end,
mocking only `solver_writer.fetch_solver_config()`/`rescore_quality_history()`
(the real recorder/oracle calls) and `hass.async_add_executor_job` (run
inline, matching the pattern already used for coordinator executor-job
tests elsewhere in this suite), not the date-handling logic under test.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.exceptions import ServiceValidationError

from custom_components.nimbus_load import services


def _fake_call(data: dict) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


def _fake_hass() -> MagicMock:
    hass = MagicMock()

    async def _run_inline(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run_inline)
    return hass


def _patched_rescore(fixed_now: datetime):
    """Patches services.dt_util.now() and solver_writer's own two calls
    the blocking closure makes, and returns the mock so callers can
    assert on it."""
    fake_rescore = MagicMock(return_value={"scored": 1})
    return (
        patch.object(services.dt_util, "now", return_value=fixed_now),
        patch(
            "custom_components.nimbus_load.solver_writer.fetch_solver_config",
            return_value={},
        ),
        patch(
            "custom_components.nimbus_load.solver_writer.rescore_quality_history",
            fake_rescore,
        ),
        fake_rescore,
    )


def test_valid_date_five_days_back_derives_back_and_passes_only_dates():
    """The happy path: a valid ISO date five days behind `now` derives
    back=5 and reaches rescore_quality_history with only_dates={date},
    matching what #1208's own test asserts about the downstream call --
    this test is the missing piece that proves the service handler
    actually produces that call shape from a real service invocation."""
    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    p_now, p_cfg, p_rescore, fake_rescore = _patched_rescore(now)
    hass = _fake_hass()
    call = _fake_call({"date": "2026-09-20"})

    with p_now, p_cfg, p_rescore:
        asyncio.run(services._async_handle_rescore_history(hass, call))

    fake_rescore.assert_called_once()
    args, kwargs = fake_rescore.call_args
    assert args[2] == 5, f"expected back=5 for a date 5 days behind now, got {args[2]}"
    assert kwargs.get("only_dates") == {"2026-09-20"}


def test_malformed_date_raises_service_validation_error():
    hass = _fake_hass()
    call = _fake_call({"date": "not-a-date"})

    with pytest.raises(ServiceValidationError, match="not an ISO date"):
        asyncio.run(services._async_handle_rescore_history(hass, call))


def test_date_today_is_rejected_as_a_day_still_in_progress():
    """back == 0 must be rejected -- today has no full-day score yet."""
    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    hass = _fake_hass()
    call = _fake_call({"date": "2026-09-25"})

    with (
        patch.object(services.dt_util, "now", return_value=now),
        pytest.raises(ServiceValidationError, match="still in progress"),
    ):
        asyncio.run(services._async_handle_rescore_history(hass, call))


def test_date_more_than_30_days_back_is_rejected():
    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    hass = _fake_hass()
    call = _fake_call({"date": "2026-08-01"})  # 55 days back

    with (
        patch.object(services.dt_util, "now", return_value=now),
        pytest.raises(ServiceValidationError, match="30"),
    ):
        asyncio.run(services._async_handle_rescore_history(hass, call))


def test_date_in_the_future_is_rejected():
    """A negative back (target after now) must not silently become a
    bulk-rescore of the wrong window -- it must be rejected the same way
    an out-of-range past date is."""
    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    hass = _fake_hass()
    call = _fake_call({"date": "2026-09-30"})

    with (
        patch.object(services.dt_util, "now", return_value=now),
        pytest.raises(ServiceValidationError),
    ):
        asyncio.run(services._async_handle_rescore_history(hass, call))


def test_no_date_still_uses_days_window_unaffected():
    """Control: the days-only path (#1216's own claim that it left this
    contract alone) is unaffected by any of the date-handling additions."""
    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    p_now, p_cfg, p_rescore, fake_rescore = _patched_rescore(now)
    hass = _fake_hass()
    call = _fake_call({"days": 5})

    with p_now, p_cfg, p_rescore:
        asyncio.run(services._async_handle_rescore_history(hass, call))

    fake_rescore.assert_called_once()
    args, kwargs = fake_rescore.call_args
    assert args[2] == 5
    assert kwargs.get("only_dates") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
