"""Dispatch's own right-hand side for a constraint P5MIN violates, compared
run by run: the evidence that a P5MIN spike is a modelling error, not a
market (#1778) (#1837).

Fixtures are public NEMWeb rows trimmed to QLD1's price and a handful of
constraints (tests/fixtures/dispatch_rhs/): a 15 Oct pair where P5MIN runs
to the cap while dispatch is ~$103/MWh, and 16 Oct pairs before and after
AEMO corrected the right-hand side.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PKG = Path(__file__).resolve().parents[3] / "custom_components" / "nimbus_load"
_spec = importlib.util.spec_from_file_location("dispatch_rhs", _PKG / "dispatch_rhs.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "dispatch_rhs"


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

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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
    assert check["flag"] is True and check["mode"] == "control_room_live"
    assert check["forecast_peak"] == 23200.0
    warnings = [r for r in caplog.records if "#1778 (diagnostic)" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING


# --- malformed and partial NEMWeb text (#1837: the paths no fixture reaches) --------

_PRICE_HEADER = "I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,INTERVENTION,RRP"
_CON_HEADER = (
    "I,DISPATCH,CONSTRAINT,5,SETTLEMENTDATE,RUNNO,CONSTRAINTID,INTERVENTION,"
    "RHS,MARGINALVALUE,VIOLATIONDEGREE"
)
_QLD_PRICE = "D,DISPATCH,PRICE,5,2026/10/15 16:10:00,1,QLD1,0,103.5"


def _csv(*lines: str) -> str:
    return "\n".join(lines)


def test_unreadable_numbers_and_times_are_none_not_errors() -> None:
    assert dr._float("n/a") is None
    assert dr._float("") is None
    assert dr._float(None) is None
    assert dr._nem_time("15 Oct 2026") is None


def test_rows_skip_a_short_row_an_intervention_and_data_before_its_header() -> None:
    text = _csv(
        _QLD_PRICE,  # before its header
        _PRICE_HEADER,
        "D,DISPATCH,PRICE,5,2026/10/15 16:10:00,1,QLD1,0",  # one field short
        "D,DISPATCH,PRICE,5,2026/10/15 16:10:00,1,QLD1,1,999.0",  # intervention
        _QLD_PRICE,
    )
    rows = list(dr._rows(text, "DISPATCH", ("PRICE",)))
    assert [r["RRP"] for _t, r in rows] == ["103.5"]


def test_dispatch_without_the_regions_price_or_a_readable_time_is_none() -> None:
    other_region = _csv(
        _PRICE_HEADER, "D,DISPATCH,PRICE,5,2026/10/15 16:10:00,1,NSW1,0,90.0"
    )
    assert dr.parse_dispatch(other_region, "QLD1") is None
    bad_time = _csv(_PRICE_HEADER, "D,DISPATCH,PRICE,5,15 Oct 16:10,1,QLD1,0,103.5")
    assert dr.parse_dispatch(bad_time, "QLD1") is None


def test_a_constraint_with_no_marginal_value_is_not_binding() -> None:
    text = _csv(
        _PRICE_HEADER,
        _QLD_PRICE,
        _CON_HEADER,
        "D,DISPATCH,CONSTRAINT,5,2026/10/15 16:10:00,1,Q>>SLACK,0,500,0,0",
        "D,DISPATCH,CONSTRAINT,5,2026/10/15 16:10:00,1,Q>>BIND,0,500,-12.5,0",
    )
    d = dr.parse_dispatch(text, "QLD1")
    assert d is not None
    assert list(d["binding"]) == ["Q>>BIND"]


def test_a_p5min_run_with_no_constraint_rows_has_none() -> None:
    assert dr.first_interval_constraints(_PRICE_HEADER) == {}
