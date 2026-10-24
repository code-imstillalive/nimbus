"""A failed solve leaves a record a later healthy cycle still carries: the
last failure's HiGHS status and time, and the counts of failures and of
Unknowns the retry rescued, readable from recorder history after the log is
gone (#1360, #1723) (#1837).
"""

from __future__ import annotations

import contextlib
import copy
from datetime import UTC, datetime

import pytest
import solver_plan
import solver_publish
import solver_shared
import solver_writer as sw
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths
from solver import lp, network


def _main(fail=False):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    base = guardrail._make_ha_get()

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        return base(entity_id)

    posted = {}
    with contextlib.ExitStack() as stack:
        mp = stack.enter_context(pytest.MonkeyPatch.context())
        mp.setattr(sw, "ha_get", ha_get)
        mp.setattr(solver_shared, "ha_get", ha_get)
        mp.setattr(sw, "ha_post_state", lambda e, s, a=None: posted.__setitem__(e, a))
        if fail:
            real = network.build_plan

            def failing(**kw):
                plan = real(**kw)
                return network._infeasible_plan(
                    plan.periods, "error", iterations=0, raw_status="Time limit reached"
                )

            mp.setattr(solver_plan.network, "build_plan", failing)
        stack.enter_context(isolated_state_paths(sw))
        sw.main()
    return posted.get("sensor.nimbus_solver_battery_forecast")


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_a_healthy_cycle_after_a_failure_still_names_it(freezer, monkeypatch):
    monkeypatch.setattr(
        solver_publish, "_LAST_FAILURE", {"raw_status": None, "at": None}
    )
    monkeypatch.setattr(lp, "UNKNOWN_RETRIES", {"attempted": 0, "recovered": 0})
    before = dict(solver_publish._CYCLE_COUNTS)
    assert _main(fail=True) is None  # the failed cycle publishes no plan
    freezer.tick(60)
    attrs = _main()["solve_diagnostics"]
    assert attrs["last_failure_raw_status"] == "Time limit reached"
    assert datetime.fromisoformat(attrs["last_failure_at"]) == datetime(
        2026, 9, 6, 10, 0, tzinfo=UTC
    )
    assert attrs["solver_failures"] == before["solver_failures"] + 1
    assert attrs["solves_attempted"] == before["solves_attempted"] + 2
    assert (attrs["unknown_retries"], attrs["unknown_retries_recovered"]) == (0, 0)
