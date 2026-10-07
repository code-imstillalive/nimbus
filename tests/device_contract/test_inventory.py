"""Spec DC-000 step 2: the measured producer/consumer/write-boundary
inventory (scripts/device_contract_baseline.py).

The committed inventory is the one measured at the pinned baseline. The
script itself runs against the current tree here, which makes two of DC-000's
gates executable now:

- repeatability: two runs on one tree are byte-identical;
- no unapproved actuation (DC-R15): every service call that can operate
  equipment lives in `solver_dispatch/`. A new one anywhere else fails here
  until it is reviewed and this rule is deliberately widened.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
REG = json.loads((HERE / "acceptance_registry.json").read_text(encoding="utf-8"))
COMMITTED = json.loads(
    (HERE / "inventory" / "8a9f503.json").read_text(encoding="utf-8")
)

_spec = importlib.util.spec_from_file_location(
    "_dc_baseline", ROOT / "scripts" / "device_contract_baseline.py"
)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)


def test_the_committed_inventory_is_the_pinned_baseline() -> None:
    assert COMMITTED["baseline"] == REG["baseline"]
    s = COMMITTED["summary"]
    assert s["config_keys"] == len(COMMITTED["config"])
    assert s["dynamic_domain_service_calls"] == 0  # every service domain resolved


def test_the_inventory_is_deterministic() -> None:
    a = json.dumps(baseline.inventory(ROOT), sort_keys=True)
    b = json.dumps(baseline.inventory(ROOT), sort_keys=True)
    assert a == b


def test_equipment_is_only_operated_from_solver_dispatch() -> None:
    inv = baseline.inventory(ROOT)
    actuating = [s for s in inv["service_calls"] if s["control_capable"]]
    assert actuating, "found no control-capable call: the scanner is broken"
    outside = [s for s in actuating if not s["module"].startswith("solver_dispatch/")]
    assert outside == [], outside
    assert inv["summary"]["dynamic_domain_service_calls"] == 0, [
        s for s in inv["service_calls"] if s["domain"] == "<dynamic>"
    ]


def test_every_config_key_is_read_somewhere() -> None:
    inv = baseline.inventory(ROOT)
    assert inv["never_read"] == []
