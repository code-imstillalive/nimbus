"""nimbus #1778, Mark Purcell's dispatch-side RHS test (16 Oct 2026),
diagnostic only. Fixtures are real NEMWeb rows, trimmed to QLD1's price and a
handful of constraints (tests/fixtures/dispatch_rhs/):

- 15 Oct: DispatchIS 16:10 with P5MIN 16:15. `CA_BRIS_59E2F7DC_1` binds in
  dispatch at ~2,950 MW; P5MIN has it at ~1,400 MW and violated, and runs to
  the $23,200 cap while dispatch is $103/MWh.
- 16 Oct: DispatchIS 10:45 with P5MIN 10:50 (ratio ~0.47, no P5MIN spike: the
  spike was in pre-dispatch, past P5MIN's horizon), then DispatchIS 10:50
  with P5MIN 10:55, the run in which AEMO corrected the RHS (ratio 1.00)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"
_spec = importlib.util.spec_from_file_location("dispatch_rhs", _PKG / "dispatch_rhs.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

FIX = Path(__file__).resolve().parent / "fixtures" / "dispatch_rhs"


def _text(prefix: str) -> str:
    (path,) = FIX.glob(prefix + "*.CSV")
    return path.read_text(encoding="utf-8")


def _check(dispatch_prefix: str, p5_prefix: str, peak_mwh: float | None) -> dict:
    return dr.compare(
        dr.parse_dispatch(_text(dispatch_prefix), "QLD1"),
        dr.first_interval_constraints(_text(p5_prefix)),
        forecast_peak_mwh=peak_mwh,
        spike_mwh=5000.0,
    )


def test_dispatch_parse_reads_the_price_and_binding_constraints() -> None:
    d = dr.parse_dispatch(_text("PUBLIC_DISPATCHIS_202610151610"), "QLD1")
    assert d is not None
    assert d["interval_end"] == "2026-10-15T16:10:00+10:00"
    assert d["rrp"] < 1000
    bris = d["binding"]["CA_BRIS_59E2F7DC_1"]
    assert bris["rhs"] > 2900 and bris["marginal_value"] < 0
    assert not bris["violation"]


def test_15_oct_flags_the_control_room_constraint() -> None:
    r = _check("PUBLIC_DISPATCHIS_202610151610", "PUBLIC_P5MIN_202610151615", 23200.0)
    assert r["flag"] is True and r["rhs_suspect"] is True
    assert r["reason"] == "suspect_forecast_rhs"
    top = r["constraints"][0]
    assert top["constraint_id"] == "CA_BRIS_59E2F7DC_1"
    assert top["suspect"] and top["ratio"] < dr.RATIO_MAX
    assert top["forecast_violation"] > 1000


def test_fcas_rows_are_never_compared() -> None:
    r = _check("PUBLIC_DISPATCHIS_202610151610", "PUBLIC_P5MIN_202610151615", 23200.0)
    assert not any(c["constraint_id"].startswith("F_") for c in r["constraints"])


def test_16_oct_evidence_without_a_p5min_spike() -> None:
    """The spike was in pre-dispatch: the evidence shows, the flag does not."""
    r = _check("PUBLIC_DISPATCHIS_202610161045", "PUBLIC_P5MIN_202610161050", 87.0)
    assert r["rhs_suspect"] is True
    assert r["flag"] is False and r["reason"] == "no_forecast_spike"
    assert r["constraints"][0]["constraint_id"] == "CA_BRIS_59E3F2B6_1"


def test_16_oct_clears_on_the_run_aemo_corrected() -> None:
    r = _check("PUBLIC_DISPATCHIS_202610161050", "PUBLIC_P5MIN_202610161055", 23200.0)
    assert r["rhs_suspect"] is False and r["flag"] is False
    assert r["reason"] == "forecast_rhs_matches_dispatch"
    bris = [c for c in r["constraints"] if c["constraint_id"] == "CA_BRIS_59E3F2B6_1"]
    assert bris and not bris[0]["suspect"]


def test_a_dispatched_spike_is_not_called_false() -> None:
    d = dr.parse_dispatch(_text("PUBLIC_DISPATCHIS_202610151610"), "QLD1")
    d = {**d, "rrp": 15000.0}
    r = dr.compare(
        d,
        dr.first_interval_constraints(_text("PUBLIC_P5MIN_202610151615")),
        forecast_peak_mwh=23200.0,
        spike_mwh=5000.0,
    )
    assert r["flag"] is False and r["rhs_suspect"] is False
    assert r["reason"] == "dispatch_spiked"


def test_no_dispatch_is_reported_not_raised() -> None:
    r = dr.compare(None, {}, forecast_peak_mwh=None, spike_mwh=5000.0)
    assert r["flag"] is False and r["reason"] == "no_dispatch"
    assert r["mode"] == "diagnostic"


def test_latest_file_name_picks_the_newest() -> None:
    listing = (
        '<a href="PUBLIC_DISPATCHIS_202610151605_0000000543014630.zip">'
        '<a href="PUBLIC_DISPATCHIS_202610151610_0000000543015379.zip">'
    )
    assert dr.latest_file_name(listing) == (
        "PUBLIC_DISPATCHIS_202610151610_0000000543015379.zip"
    )
    assert dr.latest_file_name("nothing here") is None


def test_the_p5min_sensor_publishes_the_check_and_logs_once(caplog) -> None:
    """The real sensor path: apply() with the 15 Oct pair publishes the
    attribute and logs one WARNING when the evidence appears."""
    import logging
    import sys
    from unittest.mock import MagicMock

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _solver_path  # noqa: F401
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from custom_components.nimbus_load import p5min
    from custom_components.nimbus_load import sensor_p5min as sp

    entry = MagicMock()
    entry.entry_id = "e1778rhs"
    s = sp.NimbusP5MinForecastSensor(entry, "QLD1", "0.94.461")
    s.hass = None
    p5 = _text("PUBLIC_P5MIN_202610151615")
    dispatch = dr.parse_dispatch(_text("PUBLIC_DISPATCHIS_202610151610"), "QLD1")
    with caplog.at_level(logging.INFO):
        for name in (
            "PUBLIC_P5MIN_202610151615_a.zip",
            "PUBLIC_P5MIN_202610151615_b.zip",
        ):
            s.apply(
                name,
                p5min.parse_run(p5, "QLD1"),
                dispatch=dispatch,
                forecast_first=dr.first_interval_constraints(p5),
            )
    check = s.extra_state_attributes["dispatch_rhs_check"]
    assert check["flag"] is True and check["mode"] == "diagnostic"
    assert check["forecast_peak"] == 23200.0
    warnings = [r for r in caplog.records if "#1778 (diagnostic)" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
