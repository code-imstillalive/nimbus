"""Real regression guard for a household-reported recurring Repairs entry
(2026-08-31): "sensor.nimbus_solver_quality_report no longer has a state
class" -- confirmed on BOTH devhub (right after a restart) and the
reference household's own NUC1 ("pretty sure not the first time" -- a
genuinely recurring, not one-off, symptom).

Root cause, confirmed by reading the real, deployed source (not guessed):
publish_daily_quality_report()'s own ha_post_state() call built its
attributes dict with a stray `"unit_of_measurement": None` literal and no
`"state_class"` key at all. In native mode, WITH a registered SensorEntity
handler present (ha_post_state()'s normal, expected path -- see that
function's own docstring), the entity's own `_attr_native_unit_of_
measurement`/`_attr_state_class` correctly override these stray values by
the time a live GET reads the state back, which is why the live entity has
always read back correct ("%" / "measurement") -- confirmed live on both
NUC1 and devhub via `GET /api/states/sensor.nimbus_solver_quality_report`
at the time this was investigated.

But `ha_post_state()`'s own RAW `states.async_set()` FALLBACK -- used
whenever NO handler is registered for this entity_id yet (e.g. this
function racing sensor.py's own async_setup_entry() shortly after a
restart, before the platform has finished forwarding) -- has no entity
object at all to draw a correction from; it writes these exact keys
verbatim. Whichever of the two paths is actually used at the moment
Recorder samples the state for its long-term-statistics validation, a
`None`/absent unit+state_class is what gets flagged -- explaining a
symptom that self-heals (the very next successfully-handler-routed write)
but keeps recurring (any solve cycle that happens to race the fallback
path again).

Fix: real, correct literal values ("%" / "measurement", matching
NimbusSolverQualityReportSensor's own class attributes in sensor.py)
instead of the stray None -- correct regardless of which of the two
ha_post_state() code paths is actually used, closing the gap outright
rather than depending on an entity-level override that only exists on
one of them.

Source-inspection style, matching the existing precedent in
tests/test_solver_writer_family_a_freshness_repush.py and tests/
test_solver_writer_load_total_state_consistency.py -- this function lives
inside the same ~2400-line real-network-calls file those tests already
avoid importing/executing end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from _writer_source import function_source

_SOLVER_WRITER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)


def _extract_function(src: str, def_marker: str) -> str:
    """One top-level function's full source, from whichever of the writer's
    modules now holds it.

    Was a substring slice on solver_writer.py alone, ending at the next
    column-0 `def `/`class `. nimbus issue #1301 relocated several of these
    functions into solver_reports/, so `src.index(def_marker)` raised
    `ValueError: substring not found` -- the function is still there, still
    called, still re-exported from the facade, just in a different file.

    `function_source()` resolves the function and ITS OWN module's text
    together (see tests/_writer_source.py), which is also stricter than the
    old slice: an AST end_lineno is exact, where the column-0 heuristic
    relied on these functions happening to have no nested defs.

    `src` is ignored and kept only so the call sites do not all have to
    change.
    """
    name = def_marker.removeprefix("def ").removesuffix("(")
    return function_source(name)


def test_quality_report_publish_no_longer_sends_a_null_unit():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_function(src, "def publish_daily_quality_report(")

    assert '"unit_of_measurement": None' not in block, (
        "the stray null-unit literal is back -- this is exactly what "
        "produced the recurring 'no longer has a state class' Repairs "
        "entry on both devhub and the reference household's NUC1"
    )


def test_quality_report_publish_sends_the_real_unit_and_state_class():
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    block = _extract_function(src, "def publish_daily_quality_report(")

    # Real values, matching NimbusSolverQualityReportSensor's own class
    # attributes (sensor.py) exactly -- so even the raw states.async_set()
    # fallback path (no entity object, no override available) produces a
    # correct state entirely on its own.
    assert '"unit_of_measurement": "%"' in block
    assert '"state_class": "measurement"' in block
