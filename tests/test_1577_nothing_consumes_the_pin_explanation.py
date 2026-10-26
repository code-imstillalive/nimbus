"""nimbus #1577: the infeasible-pin explanation is diagnostic only.

Insufficient or unexplained evidence must never become an action. The
structural guarantee: the helper and its record fields are reachable only
from the instrument's own log-line code, and not from the Repairs path.

The behaviour (each blocker is real, a feasible pin is never blamed) lives
in `tests/v2/solver/test_pin_blockers.py` and
`tests/v2/orchestration/test_plan_assembly.py` (#1837).
"""

from __future__ import annotations

from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"


def test_only_solver_plan_uses_the_helper_or_its_fields():
    # test_only_solver_plan_uses_the_helper_or_its_fields
    allowed = {PKG / "solver_plan.py", PKG / "solver" / "pin_blockers.py"}
    users = [
        path
        for path in PKG.rglob("*.py")
        if any(
            token in path.read_text(encoding="utf-8")
            for token in ("pin_blockers", "pinned_blockers", "pinned_participant")
        )
    ]
    assert users, "scan found nothing -- wrong root?"
    assert sorted(set(users) - allowed) == []

    # test_the_repairs_path_does_not_read_the_instrument
    text = (PKG / "setup_health.py").read_text(encoding="utf-8")
    for token in ("period0", "pin_blockers", "pinned_blockers", "#1577"):
        assert token not in text
