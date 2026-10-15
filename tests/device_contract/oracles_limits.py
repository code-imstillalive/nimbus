"""Independent reference oracles for shared limits (Spec DC-000 F02, F07).

Like `oracles.py`, nothing here imports Nimbus production code. These state
what it means for several devices to share one piece of equipment's limit
(DC-R09, #1574: a shared limit "must not be copied into independent limits
that allow their combined output to exceed the equipment rating").

A shared limit names one combined quantity. Each quantity has one formula over
its members' canonical power (Grid +import, Battery +discharge, Solar
+generation, Load +consumption), in kW over one interval:

- `circuit_power`: loads on one circuit, the sum of their consumption;
- `charger_power`: vehicles on one charger, the sum of each member's charge
  plus discharge. A charging and a discharging member both load the charger,
  so they never cancel;
- `inverter_ac_power`: solar and batteries behind one hybrid inverter, the
  magnitude of what crosses its AC terminals, generation plus discharge less
  charge. Charging from the panels absorbs generation on the DC side.

The quantity names follow the DC-003 draft (#1781), whose list of quantities is
still open for review. The physics of each formula does not depend on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

QUANTITY_KINDS = {
    "circuit_power": frozenset({"load"}),
    "charger_power": frozenset({"battery"}),
    "inverter_ac_power": frozenset({"solar", "battery"}),
}


@dataclass(frozen=True)
class Member:
    """One device's canonical power in the interval."""

    name: str
    kind: str  # "load" | "solar" | "battery"
    kw: float


@dataclass(frozen=True)
class SharedLimit:
    """A limit stated once, against one combined quantity."""

    equipment: str
    quantity: str
    devices: tuple[str, ...]
    limit_kw: float


def combined_kw(quantity: str, members: Iterable[Member]) -> float:
    """The combined quantity a shared limit constrains."""
    members = list(members)
    if quantity not in QUANTITY_KINDS:
        raise ValueError(f"unknown shared quantity {quantity!r}")
    for m in members:
        if m.kind not in QUANTITY_KINDS[quantity]:
            raise ValueError(f"{m.name}: a {m.kind} is not part of {quantity}")
    if quantity == "circuit_power":
        return sum(m.kw for m in members)
    if quantity == "charger_power":
        return sum(abs(m.kw) for m in members)
    return abs(sum(m.kw for m in members))


def within(limit: SharedLimit, members: Sequence[Member], tol: float = 1e-9) -> bool:
    """Whether the members named by `limit` respect it in this interval. Every
    named device must be present: a missing member is not a zero."""
    by = {m.name: m for m in members}
    missing = [d for d in limit.devices if d not in by]
    if missing:
        raise ValueError(f"no reading for {missing}")
    return combined_kw(limit.quantity, [by[d] for d in limit.devices]) <= (
        limit.limit_kw + tol
    )


def within_copied_limits(
    members: Iterable[Member], limit_kw: float, tol: float = 1e-9
) -> bool:
    """The defect the contract forbids: the rating copied onto each member as
    its own limit. Each member is checked alone, so their combined quantity
    can exceed the rating unnoticed."""
    return all(abs(m.kw) <= limit_kw + tol for m in members)


def limit_problems(limits: Sequence[SharedLimit], kinds: dict[str, str]) -> list[str]:
    """Structural problems with a site's shared limits: an unknown quantity, a
    device the quantity does not accept, an undeclared device, fewer than two
    members, a non-positive limit, or the same equipment and quantity limited
    twice (a combined quantity is constrained once). Empty when valid."""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for lim in limits:
        if lim.quantity not in QUANTITY_KINDS:
            problems.append(f"{lim.equipment}: unknown quantity {lim.quantity!r}")
            continue
        if len(set(lim.devices)) < 2:
            problems.append(f"{lim.equipment}: a shared limit needs two devices")
        if lim.limit_kw <= 0:
            problems.append(f"{lim.equipment}: limit must be positive")
        for d in lim.devices:
            if d not in kinds:
                problems.append(f"{lim.equipment}: undeclared device {d}")
            elif kinds[d] not in QUANTITY_KINDS[lim.quantity]:
                problems.append(
                    f"{lim.equipment}: {d} is a {kinds[d]}, not part of {lim.quantity}"
                )
        key = (lim.equipment, lim.quantity)
        if key in seen:
            problems.append(f"{lim.equipment}: {lim.quantity} limited twice")
        seen.add(key)
    return problems
