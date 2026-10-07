"""Spec DC-000 F07: the exact-once accounting oracle, independent of
production accounting code.

The spec's synthetic hour: PV 16 kW; whole-house consumption 14 kW, of which
an AC EV charges 10 kW; a DC EV outside the house total charges 2 kW; BESS
idle. Grid import is 0 kW by either valid representation. Adding the AC EV on
top of the house total gives 10 kW and must fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.device_contract.oracles import Flow, ac_side_kw, decompose, site_grid_kw

TOL = 1e-9
F07 = json.loads(
    (
        Path(__file__).parent / "fixtures" / "F07_bess_ev_accounting" / "manifest.json"
    ).read_text(encoding="utf-8")
)


def _flows(**overrides: float) -> list[Flow]:
    return [
        Flow(
            f["name"],
            f["role"],
            overrides.get(f["name"], f["kw"]),
            f.get("included_in"),
        )
        for f in F07["data_semantics"]["flows"]
    ]


def test_manifest_numbers_are_the_specs() -> None:
    kw = {f["name"]: f["kw"] for f in F07["data_semantics"]["flows"]}
    assert kw == {"pv": 16.0, "house": 14.0, "ev_ac": -10.0, "ev_dc": -2.0, "bess": 0.0}
    assert F07["comparison"]["absolute_tolerance"] == TOL


def test_inclusive_representation_balances() -> None:
    """Keep the house at 14 kW; add only the excluded DC EV."""
    assert abs(site_grid_kw(_flows()) - F07["expected"]["grid_import_kw"]) <= TOL


def test_decomposed_representation_balances_identically() -> None:
    """House residual 14 - 10 = 4 kW, then the AC EV and the DC EV once."""
    flows = _flows()
    house = next(f for f in flows if f.name == "house")
    ac = next(f for f in flows if f.name == "ev_ac")
    rest = [f for f in flows if f.name not in ("house", "ev_ac")]
    parts = decompose(house, [ac])
    assert abs(parts[0].kw - F07["expected"]["decomposed_house_residual_kw"]) <= TOL
    assert abs(site_grid_kw([*parts, *rest])) <= TOL
    assert abs(site_grid_kw([*parts, *rest]) - site_grid_kw(flows)) <= TOL


def test_the_double_counted_sum_fails_the_expectation() -> None:
    """14 + 10 + 2 - 16 = 10 kW: the AC EV added on top of a total that
    already contains it."""
    double = [Flow(f.name, f.role, f.kw, None) for f in _flows()]
    got = site_grid_kw(double)
    assert abs(got - F07["expected"]["invalid_double_count_grid_kw"]) <= TOL
    assert abs(got - F07["expected"]["grid_import_kw"]) > TOL


def test_changing_the_ac_ev_schedule_replaces_its_embedded_baseline() -> None:
    """The AC EV now charges 6 kW: remove its embedded 10 kW, add 6 kW.
    Suppressing the EV entirely is not the correction."""
    flows = _flows()
    house = next(f for f in flows if f.name == "house")
    old_ac = next(f for f in flows if f.name == "ev_ac")
    residual, _ = decompose(house, [old_ac])
    rest = [f for f in flows if f.name not in ("house", "ev_ac")]
    grid = site_grid_kw([residual, Flow("ev_ac", "battery", -6.0), *rest])
    assert abs(grid - (4.0 + 6.0 + 2.0 - 16.0)) <= TOL  # -4 kW: exporting
    suppressed = site_grid_kw([residual, *rest])
    assert abs(suppressed - grid) > TOL


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"pv": 20.0}, -4.0),  # export variant
        ({"ev_ac": 0.0, "ev_dc": 0.0, "house": 4.0}, -12.0),  # no EV flow
        ({"bess": 3.0}, -3.0),  # BESS discharging
    ],
)
def test_variants(overrides: dict, expected: float) -> None:
    assert abs(site_grid_kw(_flows(**overrides)) - expected) <= TOL


def test_declared_losses_apply_on_the_ac_side() -> None:
    """A 90 % efficient DC charger putting 2 kW into the pack draws 2.222 kW
    of AC, so the otherwise balanced site imports the loss."""
    dc_ac = ac_side_kw(-2.0, 0.9)
    assert abs(dc_ac - (-2.0 / 0.9)) <= TOL
    assert abs(site_grid_kw(_flows(ev_dc=dc_ac)) - (2.0 / 0.9 - 2.0)) <= TOL
