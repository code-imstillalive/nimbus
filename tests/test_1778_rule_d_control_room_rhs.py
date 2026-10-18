"""nimbus #1778 rule (d): Mark Purcell's dispatch-side RHS test, live for
control-room (`CA_`) constraints only.

Real NEMWeb rows (tests/fixtures/dispatch_rhs/):
- 16 Oct DispatchIS 10:45 with P5MIN 10:50: `CA_BRIS_59E3F2B6_1` binds in
  dispatch at ~2,550 MW, P5MIN has it at ~1,190 MW (ratio 0.47) and prices
  nothing high; the spike was in pre-dispatch, past P5MIN's horizon.
- 16 Oct DispatchIS 10:50 with P5MIN 10:55: AEMO corrected the RHS (1.00).

Measured on every P5MIN run of 13-16 Oct 2026 (1,152 runs, NEMWeb Archive):
`CA_` rows read 0.46-0.50 of dispatch's RHS in every false-spike run and
0.96-1.09 in all ~160 others; non-`CA_` rows (`NRM_NSW1_VIC1`, `V::N_NIL_V2`,
`#R..._RAMP`) read under 0.6 on ordinary runs every day, so they stay
diagnostic."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import dispatch_rhs as dr
from custom_components.nimbus_load import p5min, p5min_guard

FIX = Path(__file__).resolve().parent / "fixtures" / "dispatch_rhs"
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


def test_the_16_oct_pair_is_control_room_evidence() -> None:
    _run, check, _ = _pair(
        "PUBLIC_DISPATCHIS_202610161045", "PUBLIC_P5MIN_202610161050"
    )
    assert check["control_room_suspect"] is True
    assert check["constraints"][0]["constraint_id"] == "CA_BRIS_59E3F2B6_1"


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


def test_rule_d_releases_on_the_run_aemo_corrected() -> None:
    run, check, published = _pair(
        "PUBLIC_DISPATCHIS_202610161045", "PUBLIC_P5MIN_202610161050"
    )
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_without_violations(run), published, rhs=check)
    assert g.active
    run2, check2, published2 = _pair(
        "PUBLIC_DISPATCHIS_202610161050", "PUBLIC_P5MIN_202610161055"
    )
    assert check2["control_room_suspect"] is False
    g.on_run(_without_violations(run2), published2, rhs=check2)
    assert not g.active
    assert g.last_release["reason"] == p5min_guard.RELEASE_CLEAN


def test_rule_d_renews_while_the_evidence_holds() -> None:
    run, check, published = _pair(
        "PUBLIC_DISPATCHIS_202610161045", "PUBLIC_P5MIN_202610161050"
    )
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_without_violations(run), published, rhs=check)
    later = published.replace(minute=published.minute + 5)
    g.on_run({**_without_violations(run), "run_datetime": "later"}, later, rhs=check)
    assert g.renewed == later


def test_a_suspect_row_that_is_not_control_room_does_not_arm() -> None:
    """13 Oct 13:25: NRM_NSW1_VIC1 read 0.116 of dispatch's RHS at $276/MWh
    with nothing wrong in the market."""
    run, _check, published = _pair(
        "PUBLIC_DISPATCHIS_202610161050", "PUBLIC_P5MIN_202610161055"
    )
    rhs = {
        "control_room_suspect": False,
        "rhs_suspect": True,
        "dispatch_rrp": 276.0,
        "constraints": [
            {
                "constraint_id": "NRM_NSW1_VIC1",
                "dispatch_rhs": 300.0,
                "forecast_rhs": 34.8,
                "ratio": 0.116,
                "forecast_violation": 0.0,
                "suspect": True,
            }
        ],
    }
    g = p5min_guard.FalseSpikeGuard()
    g.on_run(_without_violations(run), published, rhs=rhs)
    assert not g.active


def test_rule_d_needs_normal_dispatch() -> None:
    """compare() withholds the evidence when dispatch itself spiked."""
    (path,) = FIX.glob("PUBLIC_P5MIN_202610161050*.CSV")
    dispatch = dr.parse_dispatch(_text("PUBLIC_DISPATCHIS_202610161045"), "QLD1")
    check = dr.compare(
        {**dispatch, "rrp": 15000.0},
        dr.first_interval_constraints(path.read_text(encoding="utf-8")),
        forecast_peak_mwh=None,
        spike_mwh=5000.0,
    )
    assert check["control_room_suspect"] is False


def test_threshold_zero_turns_rule_d_off() -> None:
    run, check, published = _pair(
        "PUBLIC_DISPATCHIS_202610161045", "PUBLIC_P5MIN_202610161050"
    )
    g = p5min_guard.FalseSpikeGuard(threshold_mwh=0)
    g.on_run(_without_violations(run), published, rhs=check)
    assert not g.active


def test_the_sensor_feeds_the_check_to_the_guard() -> None:
    from unittest.mock import MagicMock

    from custom_components.nimbus_load import sensor_p5min as sp

    entry = MagicMock()
    entry.entry_id = "e1778d"
    s = sp.NimbusP5MinForecastSensor(entry, "QLD1", "0.94.465")
    s.hass = None
    (path,) = FIX.glob("PUBLIC_P5MIN_202610161050*.CSV")
    text = path.read_text(encoding="utf-8")
    s.apply(
        path.stem + ".zip",
        _without_violations(p5min.parse_run(text, "QLD1")),
        dispatch=dr.parse_dispatch(_text("PUBLIC_DISPATCHIS_202610161045"), "QLD1"),
        forecast_first=dr.first_interval_constraints(text),
    )
    guard = s.extra_state_attributes[p5min_guard.ATTRIBUTE]
    assert guard["active"] is True
    assert guard["reason"] == p5min_guard.REASON_DISPATCH_RHS
