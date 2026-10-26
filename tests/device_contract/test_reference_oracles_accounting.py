"""Spec DC-000: the accounting oracles DC-003 needs, against the fixture
manifests, independent of production code.

- F05 time series: per-interval residuals and the negative-residual rule
  (DC-003 decision 3, unmeasured proposed figures);
- F05 inclusion boundaries: AC/DC, phases, quantity and sign (decision 4);
- F07 reconciliation: the five-value verdict (decisions 1 and 2);
- F07 mobility: no connection evidence is `mobility_unresolved` (decision 7);
- F08: #1465's 5 October aggregates reproduce `disagrees`, with no corrected
  EPR and no root cause claimed.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.device_contract.oracles_accounting import (
    ALL_PHASES,
    STATUSES,
    Meter,
    Party,
    VehicleInterval,
    Verdict,
    decomposition_verdict,
    inclusion_problems,
    reconcile,
    residual_series,
    vehicle_site_kwh,
)

TOL = 1e-9
FIX = Path(__file__).parent / "fixtures"


def _manifest(name: str) -> dict:
    return json.loads((FIX / name / "fixture.json").read_text(encoding="utf-8"))


F05 = _manifest("F05_nested_inclusion")
F07 = _manifest("F07_bess_ev_accounting")
F08 = _manifest("F08_1465_reconciliation")


# F05: residuals over a window ---------------------------------------------

TS = F05["time_series"]


def _series(case: dict) -> dict[str, list[float]]:
    series = {name: [kw] * TS["n_intervals"] for name, kw in TS["baseline_kw"].items()}
    for change in case["changes"]:
        for name, kw in change.items():
            if name != "t":
                series[name][change["t"]] = kw
    return series


def test_f05_window_negative_residuals_and_refusal():
    # test_f05_window_negative_residuals_and_refusal
    # test_f05_window_negative_residuals_and_refusal
    for name in sorted(TS["cases"]):
        case = TS["cases"][name]
        got = decomposition_verdict(_series(case), TS["inside"])
        assert got.negative == case["negative"]
        assert got.refused is case["refused"]

    # test_f05_residuals_sum_to_the_house_in_every_interval: Inclusive demand is unaffected by the decomposition, refused or not,
    for name in sorted(TS["cases"]):
        series = _series(TS["cases"][name])
        res = residual_series(series, TS["inside"])
        for t in range(TS["n_intervals"]):
            total = sum(r[t] for r in res.values())
            assert abs(total - series["house"][t]) <= TOL

    # test_f05_a_negative_residual_is_reported_as_it_is
    for name in [n for n, c in TS["cases"].items() if "residual_check" in c]:
        check = TS["cases"][name]["residual_check"]
        res = residual_series(_series(TS["cases"][name]), TS["inside"])
        assert abs(res[check["node"]][check["t"]] - check["kw"]) <= 1e-9

    # test_f05_inclusion_boundaries
    for name in sorted(BOUNDARIES):
        case = BOUNDARIES[name]
        got = inclusion_problems(
            _party("child", case["child"]),
            _party("parent", case["parent"]),
            case["quantity"],
        )
        assert got == case["problems"]

    # test_f07_reconciliation_verdict
    for name in sorted(RECON):
        case = RECON[name]
        imp, exp = case["reconstructed"]
        got = reconcile(
            imp,
            exp,
            _meter(case["meter"]),
            case["stale_fraction"],
            case.get("unsupported_reason"),
        )
        assert got.status == case["status"]
        if "tolerance_kwh" in case:
            assert got.tolerance_kwh is not None
            assert abs(got.tolerance_kwh - case["tolerance_kwh"]) <= TOL
        if "meter_sign" in case:
            assert got.meter_sign == case["meter_sign"]

    # test_f07_every_status_has_a_case
    assert {c["status"] for c in RECON.values()} == set(STATUSES)

    # test_f07_mobility
    for name in sorted(MOBILITY):
        case = MOBILITY[name]
        got = vehicle_site_kwh(
            [
                VehicleInterval(i["hours"], i["kw"], i["connected"], i.get("at_home"))
                for i in case["intervals"]
            ]
        )
        if isinstance(case["site_kwh"], str):
            assert got == case["site_kwh"]
        else:
            assert isinstance(got, float)
            assert abs(got - case["site_kwh"]) <= TOL

    # test_f08_reproduces_disagrees_from_the_counters
    got = _f08(OBS["meter_counters_kwh"], "positive_is_import")
    assert got.status == VERDICT["status"]
    assert got.tolerance_kwh is not None
    assert abs(got.tolerance_kwh - VERDICT["tolerance_kwh"]) <= KWH_TOL
    assert (
        abs((got.import_residual_kwh or 0.0) - VERDICT["import_residual_kwh"])
        <= KWH_TOL
    )
    assert (
        abs((got.export_residual_kwh or 0.0) - VERDICT["export_residual_kwh"])
        <= KWH_TOL
    )

    # test_f08_disagrees_whichever_meter_reading_and_sign
    # test_f08_disagrees_whichever_meter_reading_and_sign
    assert (
        _f08(OBS["meter_counters_kwh"], None).status == VERDICT["legacy_no_sign_status"]
    )
    assert (
        _f08(OBS["meter_power_integration_kwh"], "positive_is_import").status
        == VERDICT["power_integration_status"]
    )

    # test_f08_claims_no_correction
    assert F08["expected"]["corrected_epr"] == "unresolved"
    assert F08["expected"]["root_cause"] == "unresolved"


# F05: inclusion boundaries ------------------------------------------------

BOUNDARIES = F05["inclusion_boundaries"]["cases"]


def _party(name: str, raw: dict) -> Party:
    return Party(
        name,
        raw["kind"],
        raw["domain"],
        frozenset(raw["phases"]) if "phases" in raw else ALL_PHASES,
        raw.get("signed", True),
        raw.get("one_directional", False),
    )


# F07: the reconciliation verdict -------------------------------------------

RECON = F07["variants"]["reconciliation"]["cases"]


def _meter(raw: dict | None) -> Meter | None:
    if raw is None:
        return None
    return Meter(raw["positive_kwh"], raw["negative_kwh"], raw["declared_sign"])


# F07: mobility -------------------------------------------------------------

MOBILITY = F07["variants"]["mobility"]["cases"]


# F08: #1465's 5 October aggregates ------------------------------------------

OBS = F08["observed"]
VERDICT = F08["expected"]["verdict"]
KWH_TOL = F08["comparison"]["verdict_kwh_absolute_tolerance"]


def _f08(meter_kwh: dict, sign: str | None) -> Verdict:
    return reconcile(
        OBS["reconstructed_kwh"]["import"],
        OBS["reconstructed_kwh"]["export"],
        Meter(meter_kwh["import"], meter_kwh["export"], sign),
        OBS["stale_fraction"],
    )
