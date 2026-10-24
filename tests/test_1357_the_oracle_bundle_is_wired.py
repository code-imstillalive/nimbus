"""nimbus #1357 step 2: two checks that read source, kept apart until the
decision on source-reading tests (#1837). The builder itself is in
tests/v2/reports/test_controllable_load_history.py."""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import _solver_path
from solver_inputs import controllable_load_history as clh


def test_an_elapsed_window_is_resolved_without_the_in_progress_correction():
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(clh.build_oracle_controllable_loads))
    )
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_earliest_period_for_same_day_window" not in called
    assert "_resolve_hour_to_period_index" in called


def test_the_switch_is_read_from_the_config():
    sensor = pathlib.Path(_solver_path._SOLVER_PARENT).joinpath("sensor.py")
    assert (
        sensor.read_text(encoding="utf-8").count(
            "CONF_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED"
        )
        >= 3
    )
