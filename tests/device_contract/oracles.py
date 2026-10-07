"""Independent reference oracles for the device contract (Spec DC-000).

Nothing here imports Nimbus production code: an oracle that called the
function under test to derive its own expected answer would prove nothing
(DC-000, "Independent correctness"). Each oracle states the agreed semantic
result from first principles, so a production implementation can later be
checked against it.

Sign conventions are the contract's canonical ones (#1574): Grid +import,
Battery +discharge (so a charging battery or EV is negative), Solar
+generation, Load +consumption. All quantities are kW over one interval at a
single AC site boundary unless stated otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Flow:
    """One measured or declared quantity at the site boundary."""

    name: str
    role: str  # "load" | "solar" | "battery" (a charging EV is a battery)
    kw: float  # canonical sign
    # The load total that already contains this flow, when it is measured
    # inside one (an AC EV charger behind the whole-house meter). None for a
    # flow outside every load measurement.
    included_in: str | None = None


def site_grid_kw(flows: Iterable[Flow]) -> float:
    """Grid import implied by the site balance, counting each transfer once.

    grid = consumption - generation, where a battery's canonical +discharge
    is generation and its charge is consumption. A flow declared as
    `included_in` another load is already inside that total, so it is not
    added again.
    """
    grid = 0.0
    for f in flows:
        if f.included_in is not None:
            continue
        if f.role == "load":
            grid += f.kw
        elif f.role in ("solar", "battery"):
            grid -= f.kw
        else:
            raise ValueError(f"unknown role {f.role!r} for {f.name}")
    return grid


def decompose(total: Flow, children: Iterable[Flow]) -> list[Flow]:
    """Residual-plus-children: the total's own residual, then each child
    once, now outside the total. A child's load contribution is its charge
    (canonical negative battery kW) or its load kW."""
    kids = list(children)
    embedded = 0.0
    out: list[Flow] = []
    for c in kids:
        if c.included_in != total.name:
            raise ValueError(f"{c.name} is not declared inside {total.name}")
        embedded += c.kw if c.role == "load" else -c.kw
        out.append(Flow(c.name, c.role, c.kw, included_in=None))
    return [Flow(total.name + " residual", "load", total.kw - embedded), *out]


def ac_side_kw(dc_kw: float, efficiency: float) -> float:
    """AC power drawn (or delivered) for a DC transfer through a converter
    with the declared one-way efficiency: charging draws more than reaches
    the pack, discharging delivers less than leaves it."""
    if not 0 < efficiency <= 1:
        raise ValueError("efficiency must be in (0, 1]")
    return dc_kw / efficiency if dc_kw < 0 else dc_kw * efficiency


def validate_inclusion(flows: Iterable[Flow]) -> list[str]:
    """Structural problems with declared measurement inclusion: duplicate
    logical names, inclusion in an undeclared total, a flow included in
    itself, or a cycle. Empty when valid."""
    flows = list(flows)
    problems: list[str] = []
    names: dict[str, Flow] = {}
    for f in flows:
        if f.name in names:
            problems.append(f"duplicate logical id {f.name}")
        names[f.name] = f
    parent: Mapping[str, str | None] = {f.name: f.included_in for f in flows}
    for f in flows:
        if f.included_in is None:
            continue
        if f.included_in not in names:
            problems.append(f"{f.name} included in undeclared {f.included_in}")
            continue
        seen = {f.name}
        node = f.included_in
        while node is not None:
            if node in seen:
                problems.append(f"inclusion cycle through {f.name}")
                break
            seen.add(node)
            node = parent.get(node)
    return problems
