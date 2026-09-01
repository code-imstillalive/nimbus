"""Real regression guard for issues #289/#292 (Mark Purcell): Family-A
parent sensors (sensor.nimbus_solver_quality_report,
sensor.nimbus_efficiency_backtest, sensor.nimbus_counterfactual_soc) went
`unavailable` repeatedly -- continuously on v0.94.25 (#289), intermittently
("fires every ~10 minutes, self-heals") on v0.94.27 (#292) -- despite the
Solver producing fresh, `optimal` plans every 5-minute cycle the whole time.

Root cause, confirmed by reading the real, deployed code (not guessed):
each of the three `publish_*()` functions below has a cheap "already
scored" idempotency fast path -- reads back the entity's own currently-
published `latest_date` attribute, and if it matches, used to just
`return` with NOTHING published. That silently stopped refreshing the
entity's own freshness stamp (`_NimbusSolverPushSensor.update_from_
solver()`'s `_last_updated`, sensor.py) the moment a day was first
scored. After `_STALE_AFTER_SECONDS` (300s) with no new publish, the
freshness watchdog correctly marks the entity unavailable -- and HA
core's own `Entity.async_write_ha_state()` writes an EMPTY attributes
dict for an unavailable entity (real, long-standing HA core behaviour,
not a bug in this integration). The VERY NEXT idempotency check then
reads back `attributes={}`, finds no `latest_date` to match, and
recomputes+republishes from scratch -- refreshing the stamp, going
available again, holding for up to 300s, then repeating forever. That
deterministic oscillation is exactly the "fires every ~10 minutes,
self-heals" pattern both issues independently, precisely documented.

Fix: the fast path now re-pushes the SAME already-read state/attributes
via `ha_post_state()` before returning, instead of doing nothing --
matching the reference/standalone script's own already-correct
"already scored... re-pushing sensor (may have been wiped by a
restart)" behaviour, which this native in-process path had dropped.

Source-inspection style, matching the existing precedent in
tests/test_solver_writer_load_total_state_consistency.py and
tests/test_solver_writer_ha_post_state_logger_trace.py -- these three
functions live inside the same ~2400-line real-network-calls file those
tests already avoid importing/executing end-to-end; reading the real,
deployed source and asserting the fast path now calls `ha_post_state()`
with the just-read `existing` state/attributes (not a bare `return`) is
a real, if lightweight, guard against silently reverting to the
do-nothing fast path that caused both issues.
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

# (function def marker, entity_id constant name used inside it)
_TARGETS = [
    ("def publish_daily_quality_report(", "QUALITY_ENTITY_ID"),
    ("def publish_efficiency_backtest_report(", "BACKTEST_ENTITY_ID"),
    ("def publish_nimbus_only_soc_counterfactual(", "COUNTERFACTUAL_ENTITY_ID"),
]


def _extract_function(src: str, def_marker: str) -> str:
    """Return the full body of one top-level `def ...():` function,
    ending at the next top-level `def `/`class ` at column 0 -- these
    three functions have no nested defs of their own, so this is a
    reliable enough boundary."""
    start = src.index(def_marker)
    rest = src[start + len(def_marker) :]
    m = re.search(r"\n(?:def |class )", rest)
    end = start + len(def_marker) + (m.start() if m else len(rest))
    return src[start:end]


def _normalize_ws(s: str) -> str:
    """Strip ALL whitespace -- ruff format is free to wrap a long call
    across multiple lines (it does, for the counterfactual publisher
    specifically, since its entity-id constant is longer, putting the
    opening `(` on its own line before the arguments), so even a
    single-space-collapsed comparison can still land on the wrong side
    of a real newline. This still requires the same tokens in the same
    order, it just doesn't care where -- or whether -- any whitespace
    separates them."""
    return re.sub(r"\s+", "", s)


def test_each_family_a_publisher_re_pushes_on_the_already_scored_fast_path():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    for def_marker, entity_const in _TARGETS:
        block = _extract_function(src, def_marker)
        normalized_block = _normalize_ws(block)

        # The idempotency check itself must still exist -- this fix must
        # never reintroduce the "re-solve 1440 times a day" cost the
        # fast path exists to avoid. Whitespace-normalized on both sides
        # (see _normalize_ws's own docstring for why).
        idempotency_check = _normalize_ws(
            'existing.get("attributes", {}).get("latest_date") == yesterday_key'
        )
        assert idempotency_check in normalized_block, (
            f"{def_marker} lost its own already-scored idempotency check "
            "-- this would make the solver re-solve/re-sweep/re-replay an "
            "already-scored day on every single cycle"
        )

        # The real fix: ha_post_state() must be called with the SAME
        # already-read existing state/attributes, on the fast path,
        # before the early return.
        expected_repush = _normalize_ws(
            f'ha_post_state({entity_const}, existing["state"], existing["attributes"])'
        )
        assert expected_repush in normalized_block, (
            f"{def_marker} no longer re-pushes the existing state/"
            "attributes on its already-scored fast path -- this is "
            "exactly the #289/#292 regression: the entity's own "
            "freshness stamp stops refreshing, it goes unavailable "
            "after 5 minutes, HA core clears its attributes, and the "
            "next idempotency check recomputes from scratch -- forever"
        )

        # Ordering: the re-push call must appear BEFORE the early
        # `return` inside that same `if` branch, not after (which would
        # be dead code / never actually reached).
        fast_path_if = normalized_block.index(f"if{idempotency_check}:")
        repush_idx = normalized_block.index(expected_repush)
        return_idx = normalized_block.index("return", repush_idx)
        assert fast_path_if < repush_idx < return_idx, (
            f"{def_marker}'s re-push call is not correctly positioned "
            "between the fast-path `if` and its own `return` -- it must "
            "actually run on the fast path, not be dead code"
        )


def test_the_real_compute_path_is_untouched_and_still_gated_on_none():
    """The fix must be scoped to the fast path only -- the real
    recompute-and-first-publish path (`day_entry = compute_...(...)`,
    `if day_entry is None: return`) must be completely unchanged, since
    that's the genuinely correct, already-working "first score of a new
    day" behaviour neither #289 nor #292 ever implicated.

    Tolerates an interposed diagnostic log call between the `if ... is
    None:` line and its own `return` (issue #313, 2026-09-01: this None
    path now logs its reason before returning) -- the invariant this
    test actually cares about is "still gated on None, still eventually
    returns," not "returns on the very next line with nothing else
    happening in between."
    """
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    for def_marker, _entity_const in _TARGETS:
        block = _extract_function(src, def_marker)
        assert re.search(
            r"(day_entry|report) = compute_\w+\([^)]*\)\n\s*if \1 is None:\n(?:.*\n)*?\s*return",
            block,
        ), (
            f"{def_marker}'s real recompute-and-publish path looks "
            "different than expected -- confirm the #289/#292 fast-path "
            "fix didn't accidentally change the genuine first-publish "
            "behaviour too"
        )


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
