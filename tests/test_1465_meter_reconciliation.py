"""nimbus #1465 step 3: the scored day's reconstructed grid exchange is
checked against the grid meter before EPR and regret are trusted.

Built on Mark Purcell's measured 5 Oct 2026 evidence (#1465): the report
reconstructed 71.579 kWh import / 88.642 kWh export while the Sigenergy meter
recorded 32.740 / 69.530.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer as sw
from custom_components.nimbus_load.solver import meter_reconciliation as mr

HOURS = [1.0] * 24
PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _metered(import_kwh: float, export_kwh: float) -> list[float]:
    """A 24 h series importing for 12 h and exporting for 12 h."""
    return [import_kwh / 12.0] * 12 + [-export_kwh / 12.0] * 12


def _check(metered, recon_imp, recon_exp, stale=0.0, sensor="sensor.grid"):
    return mr.reconcile(
        meter_sensor=sensor,
        metered_kw=metered,
        period_hours=HOURS if metered is not None else None,
        stale_periods_pct=stale,
        reconstructed_import_kwh=recon_imp,
        reconstructed_export_kwh=recon_exp,
    )


def test_mark_5_oct_disagrees_with_the_meter():
    r = _check(_metered(32.740, 69.530), 71.579, 88.642)
    assert r["status"] == mr.DISAGREES
    assert r["metered_import_kwh"] == pytest.approx(32.740, abs=1e-6)
    assert r["metered_export_kwh"] == pytest.approx(69.530, abs=1e-6)
    assert r["import_residual_kwh"] == pytest.approx(38.839, abs=1e-3)
    assert r["tolerance_kwh"] == pytest.approx(10.227, abs=1e-3)
    assert "kWh" in r["reason"]


def test_flipping_the_meter_cannot_hide_a_double_count():
    """Read with the opposite sign, 5 Oct still leaves one direction far
    out: a flip swaps the directions, it does not change their sum."""
    r = _check(_metered(69.530, 32.740), 71.579, 88.642)
    assert r["status"] == mr.DISAGREES
    assert max(abs(r["import_residual_kwh"]), abs(r["export_residual_kwh"])) > 19.0


def test_a_meter_reading_positive_for_export_is_read_the_right_way():
    r = _check(_metered(40.0, 20.0), 20.0, 40.0)
    assert r["status"] == mr.AGREES
    assert r["meter_sign"] == "positive_is_export"


def test_agreement_within_the_declared_tolerance():
    """5 % apart, the battery power sensor's measured error on the
    reference household, sits inside the 10 % budget."""
    r = _check(_metered(30.0, 70.0), 31.5, 66.5)
    assert r["status"] == mr.AGREES
    assert r["meter_sign"] == "positive_is_import"
    assert "reason" not in r


def test_the_floor_keeps_a_quiet_day_from_failing_on_rounding():
    r = _check(_metered(1.0, 2.0), 2.5, 0.5)
    assert r["tolerance_kwh"] == mr.TOLERANCE_FLOOR_KWH
    assert r["status"] == mr.AGREES


def test_a_large_import_overcount_fails():
    r = _check(_metered(30.0, 70.0), 45.0, 70.0)
    assert r["status"] == mr.DISAGREES


def test_no_meter_is_not_checked_not_agreement():
    r = _check(None, 71.579, 88.642, stale=None, sensor=None)
    assert r["status"] == mr.NOT_CHECKED
    assert r["reconstructed_import_kwh"] == pytest.approx(71.579)


@pytest.mark.parametrize("stale", [10.5, 50.0, None])
def test_too_little_meter_history_is_not_evidence(stale):
    r = _check(_metered(32.740, 69.530), 71.579, 88.642, stale=stale)
    assert r["status"] == mr.INSUFFICIENT_COVERAGE


def test_the_tolerance_is_declared_constants_not_tuned():
    assert mr.TOLERANCE_FRACTION == 0.10
    assert mr.TOLERANCE_FLOOR_KWH == 2.0
    assert mr.MAX_STALE_PERIODS_PCT == 10.0


# --- the EPR verdict ------------------------------------------------------------


def test_a_disagreeing_meter_makes_epr_unreliable():
    assert sw._epr_reliability(True, True, None, mr.DISAGREES) is False
    assert sw._epr_reliability(None, True, None, mr.DISAGREES) is False


@pytest.mark.parametrize(
    "status", [mr.AGREES, mr.NOT_CHECKED, mr.INSUFFICIENT_COVERAGE, None]
)
def test_other_statuses_change_nothing(status):
    assert sw._epr_reliability(True, True, None, status) is True
    assert sw._epr_reliability(None, True, None, status) is None
    assert sw._epr_reliability(False, True, None, status) is False


def test_the_history_row_names_the_meter():
    day = {
        "epr_reliable": False,
        "regret_reliable": True,
        "epr_denominator_reason": None,
        "soc_discrepancy_reliable": True,
        "epr_reason": "grid_meter_disagrees",
        "meter_reconciliation": {"status": mr.DISAGREES},
    }
    assert sw._epr_reliability_code(day) == sw._RELIABILITY_METER == "m"


def test_a_soc_failure_on_the_same_day_is_named_first():
    day = {
        "epr_reliable": False,
        "regret_reliable": True,
        "epr_denominator_reason": None,
        "soc_discrepancy_reliable": False,
        "epr_reason": "achieved_soc_unreliable:disagreement",
        "meter_reconciliation": {"status": mr.DISAGREES},
    }
    assert sw._epr_reliability_code(day) == sw._RELIABILITY_SOC


def test_the_regret_card_explains_the_meter_code():
    js = (PKG / "frontend" / "nimbus-regret-card.js").read_text(encoding="utf-8")
    assert 'code === "m"' in js
    assert 'attrs.meter_reconciliation.status === "disagrees"' in js


def test_the_scorer_feeds_the_check_into_the_verdict():
    src = (PKG / "solver_reports" / "quality.py").read_text(encoding="utf-8")
    assert 'meter_reconciliation["status"],' in src
    assert '"meter_reconciliation": meter_reconciliation,' in src
    assert '"grid_meter_disagrees"' in src
    sensor = (PKG / "sensor.py").read_text(encoding="utf-8")
    assert (
        "for key in (CONF_SWITCHBOARD_GRID_METER_SENSOR, CONF_GRID_SENSOR):" in sensor
    )
