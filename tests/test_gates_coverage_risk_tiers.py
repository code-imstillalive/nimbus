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
