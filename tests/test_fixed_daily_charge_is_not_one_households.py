"""Household, 9 Oct 2026: the Fixed Daily Charge default was 1.95, the
reference household's own LocalVolts supply charge, inherited by every new
install whatever its retailer (Amber, a flat tariff, ...). The default is now
0, "not set", in both the number entity and the publish-side fallback. The
value is reporting-only: it feeds `total_cost_with_fixed_costs`, never the LP.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import const, solver_publish


def test_the_default_is_not_set():
    assert const.DEFAULT_SOLVER_FIXED_DAILY_CHARGE == 0.0


def test_the_publish_fallback_matches_the_default():
    tree = ast.parse(Path(solver_publish.__file__).read_text(encoding="utf-8"))
    fallbacks = [
        node.args[2].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "_cfg_num"
        and len(node.args) == 3
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "solver_fixed_daily_charge"
    ]
    assert fallbacks == [const.DEFAULT_SOLVER_FIXED_DAILY_CHARGE]
