"""Readiness, per function (DC-006, docs/specs/device-contract/
006-consumer-integration-and-readiness.md; nimbus #1574, DC-R14).

For a stored device document, whether each function can work and, when it
cannot, exactly why:

* `observe`  - Grid power, or Grid import and export energy;
* `forecast` - a Load with power or energy, and a trained model;
* `plan`     - observe, a whole-house demand forecast (or forecast ready),
               the Grid import price (and export price unless export is
               limited to zero), and for each Battery its SoC, power and
               capacity;
* `score`    - plan, a full scored day, and site exchange that reconciles.

Each is `ready`, `training`, `needs` (with the missing roles and what would
satisfy each) or `unsupported` (with why). **Training is a status, not a
fault** (roadmap). A binding whose data is stale or unavailable does not make
a function `needs`: its state is listed under `data`, and the consumer's own
fallback applies, as today. Dispatch is never a function here: control is
separately authorised (DC-007), and readiness never enables it.

Pure: no Home Assistant import. Its inputs are the document and plain
values the HA side gathers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from . import device_contract as dc
except ImportError:  # loaded standalone (tests)
    import device_contract as dc  # type: ignore[no-redef]

READY = "ready"
TRAINING = "training"
NEEDS = "needs"
UNSUPPORTED = "unsupported"
FUNCTIONS = ("observe", "forecast", "plan", "score")

# DC-003 reconciliation verdicts (oracles_accounting.reconcile) that block
# scoring, and the reliability reason each gives.
_RECONCILE_BLOCKS = {
    "disagrees": "site_exchange_disagrees",
    "unsupported": "site_exchange_unsupported",
    "insufficient_coverage": "site_exchange_coverage_too_sparse",
}

# How each missing role would be satisfied, in the household's words.
_SATISFY = {
    ("grid", "power"): "a sensor for the power at your grid connection",
    ("grid", "import_energy"): "your grid import energy total",
    ("grid", "export_energy"): "your grid export energy total",
    ("grid", "import_price"): "your import (buy) price",
    ("grid", "export_price"): "your export (feed-in) price",
    ("load", "power"): "your home's power use, or an energy total",
    ("load", "demand_forecast"): "a forecast of your home's use (Nimbus makes "
    "one once it has trained)",
    ("battery", "soc"): "the battery's state of charge",
    ("battery", "power"): "the battery's power",
    ("battery", "capacity"): "how much energy the battery holds, and whether "
    "that is usable or nominal",
}


def _missing(device: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {
        "device": device["id"],
        "role": role,
        "satisfy": _SATISFY.get((device["kind"], role), role),
    }


def _bound(device: Mapping[str, Any], *roles: str) -> bool:
    bindings = device.get("bindings") or {}
    return any(role in bindings for role in roles)


def _result(state: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, **extra}


def evaluate(
    document: Mapping[str, Any],
    *,
    data_states: Mapping[tuple[str, str], str] | None = None,
    models: Mapping[str, str] | None = None,
    scored_days: int = 0,
    reconciliation: str | None = None,
) -> dict[str, dict[str, Any]]:
    """{function: result} (module doc).

    `data_states[(device_id, role)]` is DC-002's state for each bound role
    (`valid`, `stale`, ...); `models[device_id]` is `trained` or `training`
    for each Load's forecaster; `reconciliation` is DC-003's daily verdict
    for site exchange, or None when not checked."""
    data_states = data_states or {}
    models = models or {}
    problems = dc.validate(dict(document))
    devices = list(document.get("devices") or [])
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for device in devices:
        by_kind.setdefault(str(device.get("kind")), []).append(device)

    def data(*kinds: str) -> list[dict[str, Any]]:
        """Bound roles whose data is not `valid` right now: listed, never a
        reason for `needs`."""
        out = []
        for (device_id, role), state in sorted(data_states.items()):
            device = next((d for d in devices if d.get("id") == device_id), None)
            if device is not None and device.get("kind") in kinds and state != "valid":
                out.append({"device": device_id, "role": role, "state": state})
        return out

    structural = [p for p in problems if p.code != "capacity_missing"]
    if structural:
        unsupported = _result(
            UNSUPPORTED,
            reason="the device setup has structural problems",
            problems=[{"code": p.code, "path": p.path} for p in structural],
        )
        return dict.fromkeys(FUNCTIONS, unsupported)

    grids = by_kind.get("grid", [])
    loads = by_kind.get("load", [])
    batteries = by_kind.get("battery", [])
    grid = grids[0] if grids else None
    house = next((d for d in loads if d.get("load_class") == "aggregate"), None)

    # observe
    if grid is not None and (
        _bound(grid, "power")
        or (_bound(grid, "import_energy") and _bound(grid, "export_energy"))
    ):
        observe = _result(READY, data=data("grid"))
    else:
        observe = _result(
            NEEDS,
            missing=[_missing(grid, "power")] if grid is not None else [],
            reason="Nimbus cannot see what flows through your grid connection",
        )

    # forecast
    measured = [d for d in loads if _bound(d, "power", "energy")]
    if not measured:
        forecast = _result(
            NEEDS,
            missing=[_missing(house, "power")] if house is not None else [],
            reason="no load is measured, so there is nothing to forecast from",
        )
    elif any(models.get(d["id"]) == "trained" for d in measured):
        forecast = _result(READY, data=data("load"))
    else:
        forecast = _result(
            TRAINING,
            reason="the load forecast is still in its first training window",
            training=sorted(d["id"] for d in measured),
        )

    # plan
    missing: list[dict[str, Any]] = []
    if observe["state"] == NEEDS:
        missing.extend(observe["missing"])
    if grid is not None:
        if not _bound(grid, "import_price", "import_price_forecast"):
            missing.append(_missing(grid, "import_price"))
        export_limit = (grid.get("constraints") or {}).get("export_limit")
        exports = not (isinstance(export_limit, (int, float)) and export_limit == 0)
        if exports and not _bound(grid, "export_price", "export_price_forecast"):
            missing.append(_missing(grid, "export_price"))
    has_demand_forecast = house is not None and _bound(house, "demand_forecast")
    if not has_demand_forecast and forecast["state"] == NEEDS:
        missing.extend(forecast["missing"])
    for battery in batteries:
        if not _bound(battery, "soc"):
            missing.append(_missing(battery, "soc"))
        if not _bound(battery, "power", "charge_power", "discharge_power"):
            missing.append(_missing(battery, "power"))
        if battery.get("capacity") is None:
            missing.append(_missing(battery, "capacity"))
    if missing:
        plan = _result(NEEDS, missing=missing, reason="the plan is missing inputs")
    elif not has_demand_forecast and forecast["state"] == TRAINING:
        plan = _result(
            TRAINING, reason="the plan waits for the load forecast to finish training"
        )
    else:
        plan = _result(READY, data=data("grid", "battery", "solar", "load"))

    # score
    if plan["state"] in (NEEDS, UNSUPPORTED):
        score = _result(NEEDS, reason="scoring needs a plan", missing=plan["missing"])
    elif plan["state"] == TRAINING or scored_days < 1:
        score = _result(TRAINING, reason="no full scored day yet")
    elif reconciliation in _RECONCILE_BLOCKS:
        # Marked, never corrected (DC-006 decision 7).
        score = _result(
            NEEDS,
            reason="site exchange does not reconcile, so a score would not be reliable",
            reliability=_RECONCILE_BLOCKS[reconciliation],
        )
    else:
        score = _result(
            READY,
            reliability=None
            if reconciliation == "agrees"
            else "site_exchange_not_checked",
        )

    return {"observe": observe, "forecast": forecast, "plan": plan, "score": score}


def ready_count(results: Mapping[str, Mapping[str, Any]]) -> int:
    return sum(1 for r in results.values() if r.get("state") == READY)
