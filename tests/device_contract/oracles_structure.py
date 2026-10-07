"""Independent reference oracles for site structure (Spec DC-000 F01, F05, F11).

No Nimbus production code is imported here (see `oracles.py`). These state the
agreed structural results of the device contract (#1574): what a minimal valid
site is, how nested measurement inclusion decomposes, and how a derived-signal
dependency graph is validated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# Roles a site may contain. Only Grid and an aggregate Load are required
# (DC-R01); Solar and Battery are optional, and nothing battery-specific is
# required of a site that has no battery.
REQUIRED_ROLES = frozenset({"grid", "load"})
OPTIONAL_ROLES = frozenset({"solar", "battery"})
BATTERY_ONLY_FIELDS = frozenset(
    {"capacity_kwh", "soc", "max_charge_kw", "max_discharge_kw"}
)


@dataclass(frozen=True)
class Device:
    id: str
    role: str
    fields: Mapping[str, object] = field(default_factory=dict)


def site_problems(devices: list[Device]) -> list[str]:
    """Structural problems with a site; empty when it is valid."""
    problems: list[str] = []
    roles = [d.role for d in devices]
    for role in sorted(REQUIRED_ROLES - set(roles)):
        problems.append(f"missing required {role}")
    for d in devices:
        if d.role not in REQUIRED_ROLES | OPTIONAL_ROLES:
            problems.append(f"{d.id}: unknown role {d.role!r}")
        if d.role != "battery":
            extra = BATTERY_ONLY_FIELDS & set(d.fields)
            if extra:
                problems.append(f"{d.id}: battery-only fields {sorted(extra)}")
    ids = [d.id for d in devices]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate logical id {dup}")
    return problems


def observed_grid_kw(
    measured_grid_kw: float | None, flows: list[tuple[str, float | None]]
) -> float | None:
    """Grid power (+import) for observation. A measured value is used as
    measured. With no grid measurement it is the balance of the other flows,
    given as `(role, canonical kW)`, only if every one of them is known: load
    adds, solar and battery (+discharge) subtract. Otherwise it is
    unresolved, never an invented zero."""
    if measured_grid_kw is not None:
        return measured_grid_kw
    if not flows or any(kw is None for _, kw in flows):
        return None
    total = 0.0
    for role, kw in flows:
        assert kw is not None
        if role == "load":
            total += kw
        elif role in OPTIONAL_ROLES:
            total -= kw
        else:
            raise ValueError(f"unknown role {role!r}")
    return total


@dataclass(frozen=True)
class Node:
    """A measured load, optionally measured inside a parent total."""

    name: str
    kw: float
    inside: str | None = None


def tree_problems(nodes: list[Node]) -> list[str]:
    """Inclusion problems: duplicates, dangling or cyclic parents, and a child
    measuring more than its parent (conflicting measurements)."""
    problems: list[str] = []
    by: dict[str, Node] = {}
    for n in nodes:
        if n.name in by:
            problems.append(f"duplicate logical id {n.name}")
        by[n.name] = n
    for n in nodes:
        seen = {n.name}
        node = n.inside
        while node is not None:
            if node not in by:
                problems.append(f"{n.name} inside undeclared {node}")
                break
            if node in seen:
                problems.append(f"inclusion cycle through {n.name}")
                break
            seen.add(node)
            node = by[node].inside
    if problems:
        return problems
    for n in nodes:
        kids = sum(c.kw for c in nodes if c.inside == n.name)
        if kids > n.kw:
            problems.append(f"children of {n.name} measure {kids} kW > {n.kw} kW")
    return problems


def inclusive_demand_kw(nodes: list[Node]) -> float:
    """Household demand: the top-level totals only. Children are displayed
    and forecast, never added on top of the total that already holds them."""
    return sum(n.kw for n in nodes if n.inside is None)


def residuals_kw(nodes: list[Node]) -> dict[str, float]:
    """Each node's own residual: its measurement less its direct children.
    The residuals sum to the inclusive demand, so a decomposition counts every
    load exactly once."""
    return {
        n.name: n.kw - sum(c.kw for c in nodes if c.inside == n.name) for n in nodes
    }


def demand_with_schedule_kw(nodes: list[Node], name: str, scheduled_kw: float) -> float:
    """Demand when a child's own consumption is replaced by a planned schedule:
    its measured baseline leaves every ancestor once and the schedule is added
    once. Suppressing the child entirely is not the same thing."""
    old = next(n.kw for n in nodes if n.name == name)
    return inclusive_demand_kw(nodes) - old + scheduled_kw


def dependency_problems(depends_on: Mapping[str, set[str]]) -> list[str]:
    """A derived or model signal may not depend on itself, directly or
    through others, and may only depend on declared signals."""
    problems: list[str] = []
    for name, deps in depends_on.items():
        if name in deps:
            problems.append(f"{name} depends on itself")
        for d in sorted(deps - set(depends_on)):
            problems.append(f"{name} depends on undeclared {d}")
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for d in sorted(depends_on.get(n, set())):
            if d == n or d not in depends_on:
                continue
            if state.get(d) == 1:
                problems.append("dependency cycle " + " -> ".join([*path, d]))
            elif d not in state:
                visit(d, [*path, d])
        state[n] = 2

    for n in sorted(depends_on):
        if n not in state:
            visit(n, [n])
    return problems


def revision_problems(consumer_revisions: Mapping[str, int]) -> list[str]:
    """Every supported consumer must read one configuration revision; a
    consumer left on an older one is reported, not silently tolerated."""
    revs = set(consumer_revisions.values())
    if len(revs) <= 1:
        return []
    newest = max(revs)
    return [
        f"{c} on revision {r}, others on {newest}"
        for c, r in sorted(consumer_revisions.items())
        if r != newest
    ]


def forecast_ready(source: str, model_trained: bool) -> bool:
    """Readiness follows capability. An external or deterministic forecast is
    usable without any trained model; only a learned forecast waits for one."""
    if source in ("external", "deterministic"):
        return True
    if source == "learned":
        return model_trained
    raise ValueError(f"unknown forecast source {source!r}")


def planner_supported(grid_connections: int) -> bool:
    """The planner supports one grid connection. Several is an explicit
    unsupported outcome, not a silent merge into one."""
    return grid_connections == 1
