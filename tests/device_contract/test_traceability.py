"""Spec DC-000 traceability gate: every P0 requirement has a fixture and an
owning spec, every referenced fixture is defined, every status is explicit,
and nothing claims a production pass before the code exists."""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
REG = json.loads((HERE / "acceptance_registry.json").read_text(encoding="utf-8"))
KD = json.loads((HERE / "known_defects.json").read_text(encoding="utf-8"))
ROADMAP_P0 = {f"DC-R{n:02d}" for n in range(1, 17)}
FIXTURES = {f"F{n:02d}" for n in range(1, 13)}
STATUSES = {
    "reference_oracle": {"not_started", "verified", "evidence_recorded"},
    "legacy_characterised": {"not_started", "recorded"},
    "production_obligation": {"pending", "passing", "failing", "deferred"},
}


def test_baseline_is_pinned() -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", REG["baseline"])


def test_every_p0_requirement_is_present_once_with_fixtures_and_owners() -> None:
    ids = [r["id"] for r in REG["requirements"]]
    assert len(ids) == len(set(ids))
    assert {r["id"] for r in REG["requirements"] if r["priority"] == "P0"} == ROADMAP_P0
    for r in REG["requirements"]:
        assert r["fixtures"], r["id"]
        assert r["owning_specs"], r["id"]
        assert all(re.fullmatch(r"DC-00[0-8]", s) for s in r["owning_specs"]), r


def test_every_fixture_is_defined_and_every_reference_resolves() -> None:
    assert set(REG["fixtures"]) == FIXTURES
    for r in REG["requirements"]:
        assert set(r["fixtures"]) <= FIXTURES, r["id"]
    covered = {f for r in REG["requirements"] for f in r["fixtures"]}
    assert covered == FIXTURES, FIXTURES - covered


def test_every_status_is_explicit_and_nothing_claims_a_production_pass() -> None:
    for fid, f in REG["fixtures"].items():
        assert set(f["status"]) == set(STATUSES), fid
        for key, value in f["status"].items():
            assert value in STATUSES[key], (fid, key, value)
        # DC-000 implements no production behaviour.
        assert f["status"]["production_obligation"] != "passing", fid


def test_a_started_oracle_has_a_manifest_that_agrees() -> None:
    for fid, f in REG["fixtures"].items():
        if f["status"]["reference_oracle"] == "not_started":
            continue
        manifests = list((HERE / "fixtures").glob(f"{fid}_*/manifest.json"))
        assert len(manifests) == 1, fid
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert manifest["identity"]["fixture"] == fid
        assert (
            manifest["execution_status"]["reference_oracle"]
            == f["status"]["reference_oracle"]
        ), fid


def test_known_defects_point_at_real_fixtures_and_claim_no_fix() -> None:
    for d in KD["defects"]:
        assert d["fixture"] in FIXTURES, d["id"]
        assert set(d["requirements"]) <= ROADMAP_P0, d["id"]
        assert d["corrected_epr"] == "unresolved"
        assert d["root_cause"].startswith("unresolved")
