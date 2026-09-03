"""Real regression guard for issue #85 (live devhub recurrence, 2026-08-27):
the existing "#85 trace" in `ha_post_state()`'s native-mode branch was
`print()`-only -- and per issue #85's own thread, a maintainer comment
already established that a bare `print()` "cannot be surfaced via HA log
API (print -> stdout, not `_LOGGER`); ignore." A fresh 2026-08-27 devhub
recurrence of the exact same symptom (`sensor.nimbus_solver_battery_
forecast` / `sensor.nimbus_household_load_total_forecast` writing at
~3x the expected per-solve frequency, per the recorder's own 16 KB-
attrs WARNING counts) could not be diagnosed further because that same
capture gap still existed -- there was still no way to pull this trace
via `ha_get_logs()`/HA's `error_log`.

Fix: mirror the existing `print()` trace to a real `logging.Logger`
(`_LOGGER = logging.getLogger(__name__)`, plain stdlib -- not an HA
import, so the standalone/cron/addon deployment's own "zero HA imports"
contract is untouched) so the SAME information becomes visible through
HA's normal logging/`error_log` machinery in native mode. Also adds a
second, WARNING-level trace specifically at the raw `states.async_set()`
fallback path, matching purcell-lab's own follow-up ask on issue #85
("a single trace line at every entry to the states.async_set() fallback
... whether or not the dispatch table routes elsewhere").

Source-inspection style, matching the existing precedent in
tests/test_solver_writer_load_total_state_consistency.py and
tests/test_solver_writer_solar_fallback_not_crash.py -- `ha_post_state()`
lives inside the same ~2400-line real-network-calls file those tests
already avoid importing/executing end-to-end; reading the real, deployed
source and asserting the logger calls are present (and print() is still
there too, for the standalone/cron/addon deployment) is a real, if
lightweight, guard against silently reverting to the print()-only trace.
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


def _extract_ha_post_state_native_branch(src: str) -> str:
    """Return just the `if _NATIVE_HASS is not None:` block inside
    `ha_post_state()`, so the assertions below can't accidentally match
    some unrelated logger/print call elsewhere in this large file."""
    marker = "def ha_post_state(entity_id: str, state, attributes: dict) -> None:"
    start = src.index(marker)
    end = src.index("\ndef ha_call_service(", start)
    return src[start:end]


def test_module_defines_a_real_stdlib_logger():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    assert "import logging" in src, (
        "solver_writer.py no longer imports stdlib logging -- the #85 "
        "trace needs a real logging.Logger to be visible via HA's "
        "error_log in native mode"
    )
    assert re.search(r"_LOGGER\s*=\s*logging\.getLogger\(__name__\)", src), (
        "solver_writer.py no longer defines a module-level _LOGGER -- "
        "see this file's own comment next to _NATIVE_HASS for why this "
        "is safe even in standalone/cron/addon mode (plain stdlib, no "
        "HA import, silent when nothing configures a handler for it)"
    )


def test_ha_post_state_mirrors_the_85_trace_to_the_logger_not_just_print():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_ha_post_state_native_branch(src)

    assert "print(" in block, (
        "ha_post_state()'s native branch dropped its print() trace -- "
        "still needed for the standalone/cron/addon deployment, which "
        "has no HA logging handler to catch a _LOGGER call"
    )
    assert "_LOGGER.debug(" in block, (
        "ha_post_state()'s native branch no longer mirrors the #85 "
        "trace to _LOGGER.debug() -- this is the whole point of this "
        "fix: making the trace visible via ha_get_logs()/HA's error_log, "
        "not just a container's stdout"
    )


def test_raw_states_async_set_fallback_has_its_own_logger_trace():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_ha_post_state_native_branch(src)

    fallback_marker = "_NATIVE_HASS.states.async_set, entity_id, state, attributes"
    assert fallback_marker in block, (
        "the raw states.async_set() fallback call site moved or was "
        "renamed -- update this test's marker to match"
    )
    fallback_idx = block.index(fallback_marker)
    preceding = block[:fallback_idx]

    assert "_LOGGER.warning(" in preceding, (
        "the raw states.async_set() fallback path has no _LOGGER.warning "
        "trace immediately before it -- this is purcell-lab's own "
        "follow-up ask on issue #85 (a trace line at every entry to this "
        "specific fallback, visible without opting into DEBUG logging "
        "first)"
    )
    # The warning trace must appear AFTER the via_handler dispatch
    # decision (i.e. it only fires when we actually fall through to the
    # raw fallback, not on every call) -- assert ordering, not just
    # presence.
    via_handler_idx = preceding.index("via_handler={handler is not None}")
    warning_idx = preceding.index("_LOGGER.warning(")
    assert warning_idx > via_handler_idx, (
        "the new _LOGGER.warning() fallback trace appears BEFORE the "
        "via_handler dispatch decision -- it must only fire once we've "
        "actually fallen through to the raw states.async_set() path, "
        "not unconditionally on every ha_post_state() call"
    )


if __name__ == "__main__":
    import sys

    # 2026-09-03: this used to just call the 3 tests directly and print
    # "OK" -- silently invisible to tests/run_all.py's own bare-function
    # harness, which parses stdout for a "N/M passed" summary line to
    # detect pass/fail. That mismatch made every genuine pass here show
    # up as "FAILED (exit 0)" in the aggregate run despite exit code 0
    # and zero assertion errors. Matches the standard pattern every other
    # bare-function test file in this suite already uses.
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
