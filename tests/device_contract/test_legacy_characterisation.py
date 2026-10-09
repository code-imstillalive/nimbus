"""Spec DC-000 step 4: legacy characterisation, kept separate from contract
acceptance (DC-000, "Two kinds of expected results").

`snapshots/legacy/F09_power_units.json` records what the production
power-unit code did at the pinned baseline, written only by
`scripts/device_contract_legacy.py`. Two checks hang off it:

- **Legacy parity.** Today's code gives the recorded output for every case.
  A change fails here until it is listed in `approved_changes.json`, so an
  intentional correction is reviewed rather than regenerated away.
- **Divergence ledger.** Where the legacy behaviour differs from the
  independent oracle, the difference must be declared in
  `contract_divergences.json` with an owner and a class (open decision or
  candidate defect). An undeclared divergence fails, and so does a declared
  one that no longer occurs, so the ledger cannot go stale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from power_input_check import is_energy_unit
from power_units import is_power_unit, power_scale_to_kw
from solver_shared import power_scale_or_none

from tests.device_contract.oracles_signals import POWER_TO_KW

HERE = Path(__file__).parent
REG = json.loads((HERE / "acceptance_registry.json").read_text(encoding="utf-8"))
LEGACY = json.loads(
    (HERE / "snapshots" / "legacy" / "F09_power_units.json").read_text(encoding="utf-8")
)
APPROVED = json.loads((HERE / "approved_changes.json").read_text(encoding="utf-8"))
DIVERGENCES = json.loads(
    (HERE / "contract_divergences.json").read_text(encoding="utf-8")
)["divergences"]


def _now(unit: Any) -> dict[str, Any]:
    return {
        "unit": unit,
        "is_power_unit": is_power_unit(unit),
        "scale_to_kw": power_scale_to_kw(unit),
        "is_energy_unit": is_energy_unit(unit),
    }


def _effective(case: dict[str, Any]) -> float | str:
    """What a reading in this unit becomes on the Solver's paths today: a
    stated unit that is not power is refused (#1562 for energy, #1643 for
    current, apparent power and unknown units, through
    `solver_shared.power_scale_or_none`); anything else is scaled. No unit
    at all is still read as kW, F09's open decision."""
    scale = power_scale_or_none(case["unit"])
    return "refused" if scale is None else scale


def _contract(unit: Any) -> float | str:
    return POWER_TO_KW.get(unit, "refused")


def test_the_record_is_at_the_pinned_baseline() -> None:
    assert LEGACY["baseline"] == REG["baseline"]
    assert LEGACY["kind"] == "legacy_characterisation"


def test_todays_code_matches_the_legacy_record_or_an_approved_change() -> None:
    approved = {
        (c["fixture"], json.dumps(c["case"]), c["field"]): c["new"]
        for c in APPROVED["changes"]
    }
    for recorded in LEGACY["cases"]:
        now = _now(recorded["unit"])
        for field, old in recorded.items():
            if now[field] == old:
                continue
            key = ("F09", json.dumps(recorded["unit"]), field)
            assert key in approved and approved[key] == now[field], (
                f"F09 {recorded['unit']!r} {field}: legacy {old!r}, now "
                f"{now[field]!r}; list it in approved_changes.json if intended"
            )


def test_every_approved_change_carries_its_own_accountability() -> None:
    """nimbus #1625 (IV&V pass): `approved_changes.json`'s own `_doc` string
    says each entry names "its requirement, the old and new values and the
    reviewer" -- but nothing checked that before this test. Reproduced:
    `{"fixture": "F09", "case": "GW", "field": "scale_to_kw", "new": 100000.0}`,
    with no `old`/`requirement`/`reviewer`, passed every other test in this
    file even with a real 10x regression injected into power_units.py's own
    gigawatt scale factor. The divergence ledger a few lines below already
    gets this right (`test_every_declared_divergence_is_owned_and_classified`);
    this is the same bar for the other half of this framework's real-code
    escape valve."""
    for c in APPROVED["changes"]:
        assert c.get("requirement"), c
        assert c.get("reviewer"), c
        assert "old" in c, c
        assert c["old"] != c["new"], c  # a no-op "change" is not a change


def test_every_divergence_from_the_contract_is_declared_and_still_real() -> None:
    computed = {
        json.dumps(c["unit"]): _effective(c)
        for c in LEGACY["cases"]
        if _effective(c) != _contract(c["unit"])
    }
    declared = {
        json.dumps(d["case"]): d["legacy"] for d in DIVERGENCES if d["fixture"] == "F09"
    }
    assert computed == declared


def test_every_declared_divergence_is_owned_and_classified() -> None:
    for d in DIVERGENCES:
        assert d["class"] in ("open_decision", "candidate_defect"), d
        assert d["owner"].startswith("DC-00"), d
        assert d["contract"] == _contract(d["case"]), d
        assert d["note"], d
