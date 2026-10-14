"""nimbus #1360 (15 Oct 2026): the reference household's 11-12 Oct episode,
41 of 427 cycles failed between 20:45 and 07:26 AEST on v0.94.448, was found
only from `solver_failures` in recorder history. Its warnings, which held
HiGHS's status, went with the next restart's log. So:

- the last failure's HiGHS status and time ride on every later healthy row;
- an Unknown that #1723's retry rescues is counted, because it is not a
  failure and its one WARNING per process is lost at the next restart.
"""

from __future__ import annotations

import _solver_path  # noqa: F401
import pytest
from solver import lp


@pytest.fixture(autouse=True)
def _fresh_counts(monkeypatch):
    monkeypatch.setattr(lp, "UNKNOWN_RETRIES", {"attempted": 0, "recovered": 0})
    monkeypatch.setattr(lp, "_UNKNOWN_RETRY_NOTED", set())


def test_every_retry_is_counted_even_after_the_one_warning():
    lp._note_unknown_retry("interior-point solve")
    lp._note_unknown_retry("interior-point solve")
    lp._note_unknown_retry("plain single-objective solve")
    assert lp.UNKNOWN_RETRIES == {"attempted": 3, "recovered": 0}


def test_a_retry_that_ends_optimal_is_counted_as_recovered():
    """Both retry paths count a rescue: the interior-point re-run and the
    calibrated solve's plain fallback."""
    from pathlib import Path

    text = Path(lp.__file__).read_text(encoding="utf-8")
    at = text.index("status = h.getModelStatus()\n    # nimbus #1360")
    block = text[at : at + 2500]
    plain = block.index('retried.status == "optimal"')
    assert block.index("_solve_highs(") < plain
    assert block.index('UNKNOWN_RETRIES["recovered"] += 1', plain) > plain
    ipm = block.index("status = _retry_unknown_with_ipm(h)")
    rescued = block.index("if status == highspy.HighsModelStatus.kOptimal:", ipm)
    assert block.index('UNKNOWN_RETRIES["recovered"] += 1', rescued) > rescued


def test_an_ordinary_optimal_solve_counts_nothing():
    p = lp.LPProblem()
    p.add_variable("x", lb=1.0, ub=10.0, cost=1.0)
    assert p.solve().status == "optimal"
    assert lp.UNKNOWN_RETRIES == {"attempted": 0, "recovered": 0}


def test_the_counts_and_the_last_failure_are_published():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "nimbus_load"
        / "solver_publish.py"
    ).read_text(encoding="utf-8")
    for line in (
        '"last_failure_raw_status": _LAST_FAILURE["raw_status"]',
        '"last_failure_at": _LAST_FAILURE["at"]',
        '"unknown_retries": lp.UNKNOWN_RETRIES["attempted"]',
        '"unknown_retries_recovered": lp.UNKNOWN_RETRIES["recovered"]',
    ):
        assert line in src
    branch = src.index("elif plan.solver_failed:")
    assert src.index(
        '_LAST_FAILURE["raw_status"] = plan.raw_status', branch
    ) < src.index("        return\n", branch)
