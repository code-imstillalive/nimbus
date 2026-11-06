"""Device contract DC-003: measurement inclusion and residuals.

`docs/specs/device-contract/003-accounting-and-shared-constraints.md`, first
slice (#2293). Given a validated DC-001 document and a reading per device, it
answers one question: what each parent measures beyond the children declared
inside it. "Circuit 1 1.6 kW: Pool pump 1.4, other 0.2" is a parent reading
of 1.6 and a residual of 0.2.

Only a *confirmed* `included_in_measurement` relationship counts. Discovery
proposes one from the Energy Dashboard's "Upstream device"
(`included_in_stat`, `device_discovery.py`) unconfirmed, and DC-003 reads
only what the household confirmed.

Results follow DC-003's rules:
- residuals sum to the parents' inclusive reading, so nothing is counted
  twice and nothing is lost (`residuals_kw()` in
  `tests/device_contract/oracles_structure.py` is the reference);
- a missing reading never becomes zero: the parent's residual is
  `residual_unresolved`;
- a negative residual is reported, never clamped: `residual_negative` below
  -max(0.1 kW, 5 % of the parent's reading), DC-003 decision 3 as proposed.

Pure: no Home Assistant import. Nothing reads it yet; the Power Flow and
Topology cards are its first readers, for display only. The Forecaster and
Solver are DC-006's, later.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NEGATIVE_FLOOR_KW = 0.1
NEGATIVE_SHARE = 0.05


def confirmed_parents(document: Mapping[str, Any]) -> dict[str, str]:
    """child id -> parent id, for every confirmed `included_in_measurement`
    relationship. A child named twice keeps its first parent; DC-001's
    validation reports the conflict."""
    out: dict[str, str] = {}
    for rel in document.get("relationships") or []:
        if rel.get("kind") != "included_in_measurement" or not rel.get("confirmed"):
            continue
        child, parent = rel.get("child"), rel.get("parent")
        if isinstance(child, str) and isinstance(parent, str) and child != parent:
            out.setdefault(child, parent)
    return out


def residuals_kw(
    parents: Mapping[str, str], readings: Mapping[str, float | None]
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Each parent's residual (its reading less its direct children's) and
    the problems found. A parent whose own reading or any child's is missing
    has no residual and a `residual_unresolved` problem."""
    children: dict[str, list[str]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    residuals: dict[str, float] = {}
    problems: list[dict[str, Any]] = []
    for parent in sorted(children):
        kids = sorted(children[parent])
        own = readings.get(parent)
        missing = [d for d in [parent, *kids] if readings.get(d) is None]
        if own is None or missing:
            problems.append(
                {"code": "residual_unresolved", "parent": parent, "missing": missing}
            )
            continue
        residual = own - sum(readings[c] or 0.0 for c in kids)
        residuals[parent] = residual
        if residual < -max(NEGATIVE_FLOOR_KW, NEGATIVE_SHARE * abs(own)):
            problems.append(
                {
                    "code": "residual_negative",
                    "parent": parent,
                    "children": kids,
                    "residual_kw": residual,
                }
            )
    return residuals, problems
