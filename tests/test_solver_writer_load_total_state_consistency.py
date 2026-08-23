"""Real regression guard for issue #100 (Mark Purcell, live health-check
finding #3): sensor.nimbus_household_load_total_forecast's own `state`
was using `summed_18_now_kw` -- a snapshot of load_kw[0] taken BEFORE
the optional live whole-house cross-check anchor can overwrite
load_kw[0] in place -- while the SAME push's own `forecast[0].value`
uses load_kw[0] AFTER that overwrite. Whenever a household configures
the whole-house cross-check sensor, this one sensor's own headline
`state` and its own `forecast[0].value` would silently disagree --
two numbers a reasonable reader (a dashboard, a health-check script)
assumes are the same thing.

`sensor.nimbus_solver_config`'s own separate `load_summed_18_now_kw`
diagnostic is DELIBERATELY left reading the pre-anchor snapshot --
its whole documented purpose is comparing two genuinely independent
forecasts (the configured load source vs. an independent whole-house
meter), not a forecast against an already-live-corrected value. This
test only guards the one real bug: the OTHER sensor's own internal
state/forecast[0] consistency.

Source-inspection style, matching the existing precedent in
tests/test_stale_devices_cleanup.py -- main()'s own real pipeline is
too large (well over 1000 lines, live ha_get/ha_post_state calls
throughout) to mock end-to-end for one narrow ordering fix; reading
the real, deployed source and asserting the exact push call uses the
post-anchor value is a real, if lightweight, guard against an
accidental revert back to the pre-anchor snapshot.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path

_SOLVER_WRITER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)


def _extract_household_load_total_push(src: str) -> str:
    """Return just the ha_post_state(...) call block that publishes
    sensor.nimbus_household_load_total_forecast, so the assertion below
    can't accidentally match some unrelated push elsewhere in this
    large file."""
    marker = 'ha_post_state(\n        "sensor.nimbus_household_load_total_forecast",'
    start = src.index(marker)
    # The call's own closing ")," for main()'s next statement is a
    # reliable enough end marker at this specific call site -- grab a
    # generous window (the fix's own explanatory comment alone runs
    # ~750 chars) and let the caller's own regex do the real work.
    return src[start : start + 1600]


def test_state_uses_the_post_anchor_load_kw_not_the_pre_anchor_snapshot():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_household_load_total_push(src)

    # The real fix: the state argument (the line right after the entity
    # id) must be round(load_kw[0], 3) -- NOT round(summed_18_now_kw, 3),
    # which is the exact pre-anchor snapshot that caused the bug.
    assert re.search(r"round\(\s*load_kw\[0\]\s*,\s*3\s*\)", block), (
        "sensor.nimbus_household_load_total_forecast's own state push "
        "no longer uses the post-anchor load_kw[0] -- regressed back "
        "toward the #100 state/forecast[0] inconsistency bug"
    )
    assert "round(summed_18_now_kw, 3)," not in block, (
        "sensor.nimbus_household_load_total_forecast's own state push "
        "is using the pre-anchor summed_18_now_kw snapshot again -- "
        "this is the exact #100 bug (state and forecast[0].value can "
        "silently disagree whenever a whole-house cross-check sensor "
        "is configured)"
    )


def test_solver_config_diagnostic_still_deliberately_uses_the_pre_anchor_snapshot():
    """The OTHER, unrelated use of summed_18_now_kw (the solver_config
    cross-check diagnostic) must NOT be touched by the fix above -- its
    whole point is comparing two genuinely independent forecasts, not
    a forecast against an already-live-corrected value. If a future
    edit "fixes" this one too, it silently breaks the cross-check
    diagnostic's own real purpose."""
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    assert '"load_summed_18_now_kw": round(summed_18_now_kw, 3),' in src


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
