"""Rule (d) of the false-spike guard: a control-room (`CA_`) constraint
whose P5MIN right-hand side reads about half of dispatch's arms the guard
even without a forecast violation, renews while the evidence holds and
releases on the corrected run; other constraints stay diagnostic (#1778)
(#1837).

Fixtures are public NEMWeb rows (tests/fixtures/dispatch_rhs/).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import dispatch_rhs as dr
from custom_components.nimbus_load import p5min, p5min_guard

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "dispatch_rhs"
NEM = p5min.NEM_TIME


def _text(prefix: str) -> str:
    (path,) = FIX.glob(prefix + "*.CSV")
    return path.read_text(encoding="utf-8")


def _pair(dispatch_prefix: str, p5_prefix: str) -> tuple[dict, dict, datetime]:
    """(run, compare() result, published) as the sensor builds them."""
    (path,) = FIX.glob(p5_prefix + "*.CSV")
    text = path.read_text(encoding="utf-8")
    run = p5min.parse_run(text, "QLD1")
    assert run is not None
    top = p5min.peak(run["forecast"])
    check = dr.compare(
        dr.parse_dispatch(_text(dispatch_prefix), "QLD1"),
        dr.first_interval_constraints(text),
        forecast_peak_mwh=top["value"] * 1000.0 if top else None,
        spike_mwh=5000.0,
    )
    published = p5min.published_at(path.stem + ".zip")
    assert published is not None
    return run, check, published


def _without_violations(run: dict) -> dict:
    """The run as if P5MIN bound the wrong limit without breaking it: the
    case rule (c) cannot see and rule (d) exists for."""
    return {**run, "violated": []}


def test_rule_d_arms_without_a_forecast_violation() -> None:
    run, check, published = _pair(
        "PUBLIC_DISPATCHIS_202610161045", "PUBLIC_P5MIN_202610161050"
    )
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_without_violations(run), published)
    assert not g.active, "without rule (d) nothing arms on this run"
    g.on_run(_without_violations(run), published, rhs=check)
    assert g.active and g.reason == p5min_guard.REASON_DISPATCH_RHS
    assert g.detail["constraint_id"] == "CA_BRIS_59E3F2B6_1"
    assert g.detail["ratio"] < dr.RATIO_MAX
    assert g.detail["dispatch_rrp"] < dr.NORMAL_DISPATCH_MWH
    assert "against dispatch's" in g.describe(run)
