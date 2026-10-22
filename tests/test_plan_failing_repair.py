"""A Repair when Nimbus cannot make a plan (household, 22 Oct 2026). Before,
a solver that kept failing showed only in the log and a counter. Now, after
PLAN_FAILING_AFTER cycles in a row without an optimal plan, a Repair says
for how long, whether the solver gave up or proved no plan meets the
settings, and what Nimbus does meanwhile; it clears on the next good plan
and is refreshed the moment that state flips."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import solver_publish
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import setup_health as sh

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
T0 = datetime.fromisoformat("2026-10-22T19:30:00+10:00")


def _cycles(*statuses):
    solver_publish._note_plan_outcome(SimpleNamespace(status="optimal"), T0)
    for i, status in enumerate(statuses):
        plan = SimpleNamespace(status=status, raw_status="Time limit reached")
        solver_publish._note_plan_outcome(plan, T0 + timedelta(minutes=i))
    return solver_publish.failed_streak()


def _kinds(streak):
    issues = sh.evaluate_health(
        options={}, forecasts=[], hub_up_for=timedelta(0), failed_streak=streak
    )
    return {i.kind: i for i in issues}


def test_the_streak_counts_failed_cycles_and_resets_on_a_good_plan():
    streak = _cycles("error", "error", "infeasible")
    assert streak["count"] == 3
    assert streak["since"] == T0.isoformat()  # the first failure, kept
    assert streak["status"] == "infeasible"  # the latest
    solver_publish._note_plan_outcome(SimpleNamespace(status="optimal"), T0)
    assert solver_publish.failed_streak()["count"] == 0


def test_no_repair_before_five_failed_cycles():
    assert not {sh.KIND_PLAN_FAILING_ERROR, sh.KIND_PLAN_FAILING_INFEASIBLE} & set(
        _kinds(_cycles(*["error"] * (sh.PLAN_FAILING_AFTER - 1)))
    )


def test_a_solver_that_keeps_giving_up_gets_the_error_repair():
    kinds = _kinds(_cycles(*["error"] * sh.PLAN_FAILING_AFTER))
    issue = kinds[sh.KIND_PLAN_FAILING_ERROR]
    assert issue.key == sh.KIND_PLAN_FAILING
    assert issue.placeholders == {
        "cycles": "5",
        "since": "19:30",
        "reason": "Time limit reached",
    }


def test_a_proved_infeasible_plan_gets_its_own_wording():
    kinds = _kinds(_cycles(*["infeasible"] * 6))
    assert sh.KIND_PLAN_FAILING_INFEASIBLE in kinds
    assert sh.KIND_PLAN_FAILING_ERROR not in kinds


def test_no_streak_known_means_no_repair():
    assert sh.KIND_PLAN_FAILING_ERROR not in _kinds(None)


def test_both_repairs_have_their_text():
    for name in ("strings.json", "translations/en.json"):
        issues = json.loads((ROOT / name).read_text(encoding="utf-8"))["issues"]
        for kind in (sh.KIND_PLAN_FAILING_ERROR, sh.KIND_PLAN_FAILING_INFEASIBLE):
            text = issues[kind]["title"] + issues[kind]["description"]
            for placeholder in ("{cycles}", "{since}", "{reason}"):
                assert placeholder in text, (name, kind, placeholder)
            assert "clears itself" in issues[kind]["description"]


def test_the_streak_is_read_only_once_the_solver_has_loaded_it():
    with patch.dict(sys.modules, {"solver_publish": None}):
        sys.modules.pop("custom_components.nimbus_load.solver_publish", None)
        assert sh._solver_failed_streak() is None
    _cycles("error")
    assert sh._solver_failed_streak()["count"] == 1


def test_the_repairs_refresh_only_when_the_failing_state_flips():
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="E1")
    refresh = AsyncMock()
    with patch.object(sh, "async_refresh_health", refresh):
        _cycles(*["error"] * 4)
        asyncio.run(sh.async_note_solve_outcome(hass, entry))
        assert refresh.await_count == 0  # not failing yet, and was not
        _cycles(*["error"] * 5)
        asyncio.run(sh.async_note_solve_outcome(hass, entry))
        asyncio.run(sh.async_note_solve_outcome(hass, entry))
        assert refresh.await_count == 1  # flipped to failing once
        _cycles()
        asyncio.run(sh.async_note_solve_outcome(hass, entry))
        assert refresh.await_count == 2  # and back
