"""`sensor.nimbus_solver_stochastic`: the two-stage stochastic solve on an
entity of its own (nimbus #1797).

The plan sensor carries the solve's `stochastic` summary and per-scenario
`stochastic_paths`, but on an install whose plan sensor is overwritten by a
mirrored copy of another install's (devhub), a card reading the plan sensor
shows the OTHER install's solve. This sensor is fed from the plan's own
publish in this process (`dispatch_to_flattened`, like the per-battery SoC
sensors), so it is always this install's solve.

- **State:** a one-line headline -- "Agrees with the main plan", "Holds
  2.2 kW more in the battery", "Main plan: no solar spread", "Not running".
  History keeps it; it is short and changes only when the story does.
- **`stochastic`:** the solve's summary, as the plan publishes it.
- **`forecast`:** one row per plan period -- `time`, `hours`, the main plan's
  `soc_pct` as `main`, and each solar day's battery % as `p10` / `p50` /
  `p90` -- the main plan alone when there is no stochastic plan. The
  Stochastic block's chart draws it.

Both attributes are projections and stay out of the recorder.
"""

from __future__ import annotations

from typing import Any

from .sensor_flattened import FlattenedAttrSpec, _FlattenedAttributeSensor

# Plain words for the summary's fallback reasons (solver_plan.py).
_REASONS = {
    "no_solar_band": "no solar spread",
    "solar_band_not_calibrated": "solar band not calibrated yet",
    "shadow_solve_pending": "first check running",
    "deterministic_not_optimal": "main plan did not solve",
    "thermal_guarantee_relaxed": "would relax a hot-water deadline",
    "nonanticipativity_violated": "scenarios disagreed on now",
    "scenario_cost_not_finite": "a scenario cost was not a number",
    "direction_declined": "would charge and discharge at once",
    "direction_still_two_way": "would charge and discharge at once",
}
# Below this a stage-1 difference is the same decision (kW).
_AGREE_KW = 0.05


def headline(summary: Any) -> str:
    """The sensor's state, from the plan's `stochastic` summary."""
    if not isinstance(summary, dict):
        return "Not running"
    # A refused plan says why first: a drive cycle can solve optimally and
    # still use the main plan (an uncalibrated band, a weaker plan).
    if summary.get("status") == "optimal" and not summary.get("fallback_reason"):
        stoch = summary.get("stage1_battery_kw")
        main = summary.get("deterministic_period0_battery_kw")
        if stoch is None or main is None:
            return "Hedge computed"
        diff = float(stoch) - float(main)
        if abs(diff) < _AGREE_KW:
            return "Agrees with the main plan"
        # Battery kW is discharge-positive: lower keeps more energy in.
        if diff < 0:
            return f"Holds {abs(diff):.1f} kW more in the battery"
        return f"Releases {diff:.1f} kW more from the battery"
    reason = str(summary.get("fallback_reason") or "")
    if reason.startswith("cooldown_after:"):
        return "Main plan: waiting to retry"
    if reason.startswith("error:"):
        return "Main plan: check failed"
    return "Main plan: " + _REASONS.get(reason, reason.replace("_", " ") or "skipped")


def scenario_rows(source: dict) -> list[dict[str, Any]]:
    """The chart's rows: the plan's own periods with each solar day's
    battery % beside the main plan's. Without stochastic paths the rows carry
    the main plan alone, so the chart shows what is actually driving rather
    than nothing; the headline says why."""
    paths = source.get("stochastic_paths")
    soc = paths.get("soc_pct") if isinstance(paths, dict) else None
    if not isinstance(soc, dict):
        soc = {}
    plan = source.get("forecast")
    if not isinstance(plan, list):
        return []
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(plan):
        if not isinstance(row, dict):
            continue
        out: dict[str, Any] = {
            "time": row.get("time"),
            "hours": row.get("hours"),
            "main": row.get("soc_pct"),
        }
        for label, series in soc.items():
            out[label] = series[i] if i < len(series) else None
        rows.append(out)
    return rows


class NimbusStochasticSensor(_FlattenedAttributeSensor):
    """This install's stochastic solve: headline, summary, chart rows."""

    _unrecorded_attributes = _FlattenedAttributeSensor._unrecorded_attributes | {
        "forecast",
        "stochastic",
    }

    def __init__(self, entry: Any, sw_version: str | None) -> None:
        super().__init__(
            entry,
            sw_version,
            FlattenedAttrSpec(
                source_key="headline",
                name="Stochastic",
                entity_id_suffix="stochastic",
                entity_category=None,
                device_class=None,
                state_class=None,
                unit_of_measurement=None,
                suggested_display_precision=None,
            ),
        )

    def update_from_parent(
        self, attributes: dict, parent_attributes: dict | None = None
    ) -> None:
        source = parent_attributes if parent_attributes is not None else attributes
        summary = source.get("stochastic")
        self._extra_attrs = {
            "stochastic": summary,
            "forecast": scenario_rows(source),
        }
        super().update_from_parent({"headline": headline(summary)})


def create(entry: Any, sw_version: str | None) -> list[NimbusStochasticSensor]:
    return [NimbusStochasticSensor(entry, sw_version)]
