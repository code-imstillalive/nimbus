"""Every module has a risk tier (#1837, question 1).

`scripts/coverage_by_risk.py` maps each module of the integration to a tier
and refuses to report while one is unmapped. This guard fails on the PR that
adds a module without a tier, rather than at the next report run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import coverage_by_risk as cbr


def test_every_module_has_a_risk_tier():
    unmapped = [m for m in cbr.modules() if cbr.tier_of(m) is None]
    assert not unmapped, f"add these to RISK_TIERS: {unmapped}"


def test_an_untiered_module_is_caught():
    # Planted: a module no pattern matches is reported, so the guard above
    # can fail.
    assert cbr.tier_of("brand_new_module.py") is None
    assert cbr.tier_of("solver/brand_new.py") is None


def test_issue_pins_are_told_apart_from_designed_tests():
    assert cbr.test_kind("tests/test_1703_run_hold.py::test_x|run") == "pin"
    assert cbr.test_kind("tests/v2/solver/test_2045_x.py::t|run") == "pin"
    assert cbr.test_kind("tests/v2/solver/test_plan_features.py::t|run") == "designed"
    assert cbr.test_kind("golden:quiet_day") == "golden"


def test_pin_only_counts_executed_statements_only():
    """#1837: the first report read 302% pin-only on number.py. Lines only an
    arc or another copy's path names are not statements of the module, so
    the share is of covered lines and never above 100%."""
    pin = "tests/test_1703_run_hold.py::test_x|run"
    designed = "tests/v2/solver/test_plan.py::test_y|run"
    contexts = {
        1: {pin},
        2: {pin, designed},
        3: {designed},
        -4: {pin},  # an arc's exit, not a line
        99: {pin},  # reached under another path only, not executed here
    }
    assert cbr.pin_only_lines([1, 2, 3], contexts) == 1
    assert cbr.pin_only_lines([], contexts) == 0


def test_gaps_list_only_the_flagged_modules_missed_lines():
    r = {
        "modules": [
            {"module": "solver_dispatch/guard.py", "flags": ["lines < 90%"]},
            {"module": "time.py", "flags": []},
        ]
    }
    measured = {
        "solver_dispatch/guard.py": {
            "missing_lines": [12, 13],
            "missing_branches": [[11, 12], [40, -38]],
        },
        "time.py": {"missing_lines": [5], "missing_branches": []},
    }
    assert cbr.gaps(r, measured) == {
        "solver_dispatch/guard.py": {
            "flags": ["lines < 90%"],
            "missing_lines": [12, 13],
            "missing_branches": [[11, 12], [40, -38]],
            "pin_only_by_test": {},
        }
    }


def test_pin_only_lines_are_named_by_the_pin_that_reaches_them():
    ctx = {
        3: {"tests/test_1703_run_hold.py::test_a|run"},
        4: {
            "tests/test_1703_run_hold.py::test_a|run",
            "tests/test_1801_x.py::test_b|run",
        },
        5: {"tests/test_1703_run_hold.py::test_a|run", "tests/v2/test_y.py::t|run"},
    }
    assert cbr.pin_only_by_test([3, 4, 5, 6], ctx) == {
        "tests/test_1703_run_hold.py": [3, 4],
        "tests/test_1801_x.py": [4],
    }
