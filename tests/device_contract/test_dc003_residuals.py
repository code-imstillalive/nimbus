"""DC-003, first slice (#2293): residuals of confirmed measurement inclusion.

The production `device_accounting.py` against the independent oracle
(`oracles_structure.residuals_kw`) on F05, the nested inclusion fixture: a
14 kW house holding a 6 kW circuit holding a 2 kW pool. Each parent's
residual is its reading less its direct children; residuals and leaves sum
to the inclusive reading. A missing reading is unresolved, never zero; a
negative residual is reported, never clamped; an unconfirmed relationship
(as discovery proposes one from the Energy Dashboard) counts for nothing.

Pure: device_accounting.py imports nothing from Home Assistant.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tests.device_contract.oracles_structure import Node
from tests.device_contract.oracles_structure import residuals_kw as oracle_residuals

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
_spec = importlib.util.spec_from_file_location("_dc003", _PKG / "device_accounting.py")
acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acc)  # type: ignore[union-attr]

F05 = json.loads(
    (
        Path(__file__).parent / "fixtures" / "F05_nested_inclusion" / "fixture.json"
    ).read_text(encoding="utf-8")
)


def _document(nodes, confirmed=True):
    return {
        "relationships": [
            {
                "kind": "included_in_measurement",
                "child": n["name"],
                "parent": n["inside"],
                "quantity": "consumption",
                "confirmed": confirmed,
            }
            for n in nodes
            if n.get("inside")
        ]
    }


def test_dc_r08_f05_residuals_match_the_oracle_and_never_hide_a_problem():
    nodes = F05["data_semantics"]["nodes"]
    readings = {n["name"]: n["kw"] for n in nodes}
    parents = acc.confirmed_parents(_document(nodes))
    got, problems = acc.residuals_kw(parents, readings)

    oracle = oracle_residuals(
        [Node(n["name"], n["kw"], n.get("inside")) for n in nodes]
    )
    assert got == {p: oracle[p] for p in got}
    assert got == {"house": 8.0, "circuit": 4.0}
    assert problems == []
    # Residuals plus leaves are the inclusive reading: nothing counted twice.
    assert (
        sum(got.values()) + readings["pool"] == F05["expected"]["inclusive_demand_kw"]
    )

    # An unconfirmed relationship (discovery's proposal) counts for nothing.
    assert acc.confirmed_parents(_document(nodes, confirmed=False)) == {}

    # A missing reading is unresolved, never computed as zero.
    got, problems = acc.residuals_kw(parents, {**readings, "pool": None})
    assert "circuit" not in got
    assert problems == [
        {"code": "residual_unresolved", "parent": "circuit", "missing": ["pool"]}
    ]

    # A child above its parent is reported, never clamped (F05's conflicting
    # case: a 7 kW pool inside a 6 kW circuit).
    conflicting = F05["cases"]["conflicting"]
    got, problems = acc.residuals_kw(
        acc.confirmed_parents(_document(conflicting)),
        {n["name"]: n["kw"] for n in conflicting},
    )
    assert got == {"circuit": -1.0}
    assert [p["code"] for p in problems] == ["residual_negative"]

    # Inside the tolerance (-max(0.1 kW, 5 %)) a small negative is noise.
    got, problems = acc.residuals_kw({"pool": "circuit"}, {"circuit": 6.0, "pool": 6.2})
    assert round(got["circuit"], 6) == -0.2 and problems == []
