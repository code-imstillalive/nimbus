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

import bisect
from datetime import datetime
from typing import Any

from .sensor_flattened import FlattenedAttrSpec, _FlattenedAttributeSensor

# Plain words for the summary's fallback reasons (solver_plan.py).
_REASONS = {
    "no_solar_band": "no solar spread",
    "solar_band_not_calibrated": "solar band not calibrated yet",
    "shadow_solve_pending": "first check running",
    "deterministic_not_optimal": "main plan did not solve",
    "deterministic_too_slow": "too slow to check",
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
        if abs(float(stoch) - float(main)) < _AGREE_KW:
            return "Agrees with the main plan"
        # Both actions in plain words: a difference alone misleads when both
        # plans charge (devhub, 17 Oct: "releases 36.8 kW more" meant
        # "charges 3.2 kW where the main plan charges 40").
        words = f"{_act(float(stoch))} where the main plan {_act(float(main))}"
        return words[0].upper() + words[1:]
    reason = str(summary.get("fallback_reason") or "")
    if reason.startswith("cooldown_after:"):
        return "Main plan: waiting to retry"
    if reason.startswith("error:"):
        return "Main plan: check failed"
    return "Main plan: " + _REASONS.get(reason, reason.replace("_", " ") or "skipped")


def _act(kw: float) -> str:
    """A battery command in words; battery kW is discharge-positive."""
    if kw > _AGREE_KW:
        return f"discharges {kw:.1f} kW"
    if kw < -_AGREE_KW:
        return f"charges {-kw:.1f} kW"
    return "holds"


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
    ends = _path_ends(paths, soc)
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(plan):
        if not isinstance(row, dict):
            continue
        out: dict[str, Any] = {
            "time": row.get("time"),
            "hours": row.get("hours"),
            "main": row.get("soc_pct"),
        }
        row_end = _row_end(row) if ends is not None else None
        for label, series in soc.items():
            if ends is None:
                out[label] = series[i] if i < len(series) else None
            else:
                out[label] = _at(ends, series, row_end)
        rows.append(out)
    return rows


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _row_end(row: dict) -> float | None:
    start = _ts(row.get("time"))
    try:
        hours = float(row.get("hours") or 0.0)
    except (TypeError, ValueError):
        return None
    return None if start is None else start + hours * 3600.0


def _path_ends(paths: Any, soc: dict) -> list[float] | None:
    """The paths' own period ends, as timestamps, when every path has one
    per value; else None (a path published without times, read by position).
    Household, 20 Oct 2026: the shadow solve's paths are minutes older than
    the plan's rows, so they are placed by time, not by row number."""
    raw = paths.get("ends") if isinstance(paths, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    ends = [_ts(t) for t in raw]
    if any(e is None for e in ends) or any(
        not isinstance(v, list) or len(v) != len(ends) for v in soc.values()
    ):
        return None
    return ends  # type: ignore[return-value]


def _at(ends: list[float], series: list, when: float | None) -> float | None:
    """The path's battery % at `when`, straight-line between its own period
    ends; None outside them (before the solve's first period ended, or past
    its horizon)."""
    if when is None or when < ends[0] or when > ends[-1]:
        return None
    j = bisect.bisect_left(ends, when)
    if ends[j] == when or j == 0:
        return series[j]
    a, b = series[j - 1], series[j]
    if a is None or b is None:
        return None
    frac = (when - ends[j - 1]) / (ends[j] - ends[j - 1])
    return round(a + (b - a) * frac, 1)


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
