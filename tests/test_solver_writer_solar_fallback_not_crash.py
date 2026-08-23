"""Real regression guard for issue #115 (Mark Purcell, a real
independent installer's own live health-check, 2026-08-24): main()
used to `raise RuntimeError` when every configured solar forecast
source failed to produce data this cycle -- a condition that recurs
every single night on every solar install (0.0 kW solar overnight is
the correct real value, not a failure). This crashed the solver for
~470 cycles / ~8 hours overnight on a real install, refusing to
re-optimise against changing overnight prices and refusing to recover
from an unrelated entity going unavailable until the next daylight
cycle.

Source-inspection style, matching the existing precedent in
tests/test_stale_devices_cleanup.py and tests/test_solver_writer_
load_total_state_consistency.py -- main()'s own real pipeline (this
block is itself a nested closure inside main(), well over 1000 lines,
live ha_get/ha_post_state calls throughout) is too large to mock
end-to-end for one narrow fallback-behaviour fix; reading the real,
deployed source and asserting the crash is gone (replaced by a real,
loud-but-non-fatal fallback) is a real, if lightweight, guard against
an accidental revert.
"""

from __future__ import annotations

import re
from pathlib import Path

_SOLVER_WRITER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)


def _extract_no_solar_data_block(src: str) -> str:
    """Return just the `if not solar_values:` block, so the assertions
    below can't accidentally match some unrelated `raise`/`if not ...:`
    elsewhere in this large file."""
    marker = "    if not solar_values:"
    start = src.index(marker)
    # Generous window -- the real fix's own explanatory comment plus
    # the fallback assignment together run well over 2000 chars (the
    # comment alone runs ~1350 chars; verified this window covers all
    # three np.zeros() assignments, not just the first).
    return src[start : start + 2400]


def test_no_solar_data_no_longer_raises_a_runtime_error():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_no_solar_data_block(src)
    # The exact old statement (with the opening paren) -- NOT a bare
    # "raise RuntimeError" substring check, which would false-positive
    # against the fix's own explanatory comment describing the removed
    # behaviour in prose (this test caught exactly that on its own
    # first run -- a real bug in the test itself, not the fix).
    assert "raise RuntimeError(" not in block, (
        "the no-solar-data path is raising RuntimeError again -- this is "
        "the exact #115 regression (the solver refuses to solve at all "
        "every single night, ~8h/night on a real install, instead of "
        "solving with a real, honest 0.0 kW solar placeholder)"
    )


def test_no_solar_data_falls_back_to_a_real_zero_array_not_none():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_no_solar_data_block(src)
    # The real fix: solar_values/solar_lowers/solar_uppers get
    # reassigned to a genuine [n_periods]-length zero array, so the
    # rest of the function's own len(solar_values) == 1 branch (right
    # after this block) runs exactly the same way it would for any
    # other single-source night with legitimately all-zero real data.
    assert re.search(r"solar_values\s*=\s*\[np\.zeros\(n_periods\)\]", block), (
        "solar_values is no longer being set to a real [n_periods] zero "
        "array on the no-data fallback path"
    )
    assert re.search(r"solar_lowers\s*=\s*\[np\.zeros\(n_periods\)\]", block)
    assert re.search(r"solar_uppers\s*=\s*\[np\.zeros\(n_periods\)\]", block)


def test_no_solar_data_still_logs_a_loud_warning():
    """The fix must not silently swallow the condition -- a genuine
    all-sources-down scenario during real daylight hours should still
    be visible in the log, same discipline as every other optional-
    input fallback in this file (e.g. the load-forecast equivalent)."""
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_no_solar_data_block(src)
    assert "WARN" in block
    assert "file=sys.stderr" in block


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
    import sys

    sys.exit(1 if failed else 0)
