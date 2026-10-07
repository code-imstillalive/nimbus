"""Does the scored day's reconstructed grid exchange match the grid meter?
(nimbus #1465, step 3 of Mark Purcell's recommendation.)

The quality report never measures grid import and export. It reconstructs
them as load + battery charge - battery discharge - solar
(`regret.evaluate_realized_cost*`) and prices that. If a transfer is counted
twice -- on 5 Oct 2026, AC EV charging appears to have been added on top of
a house-load signal that already contained it -- the reconstruction still
balances on its own terms and EPR and regret look valid. Mark's install
showed 71.579 kWh reconstructed import against a 32.740 kWh meter delta.

This compares the reconstruction with the meter before the day's score is
trusted. It changes no number. A disagreement marks EPR and regret
unreliable, with this check named as the reason.

**The tolerance is declared here, before any day is evaluated** (the rule
Spec DC-000 sets for real-meter reconciliation). Each direction may differ by
`TOLERANCE_FRACTION` of the metered throughput (import + export), and never
less than `TOLERANCE_FLOOR_KWH`:

- 10 %, because the reference household's battery power sensor alone reads
  5.3 % low against its BMS counters (a settled, measured fact), and meter
  accuracy, half-hourly period means and timestamp alignment add to that on
  both sides;
- a 2 kWh floor, so a quiet day with a few kWh of exchange is not failed by
  rounding.

5 Oct sits far outside it: a 38.8 kWh import gap against a 10.2 kWh budget.

**The meter's sign is not known.** Nimbus has never asked which way a grid
meter reads. So both readings are tried (positive = import, then positive =
export) and the better fit is reported, together with the sign it used. This
cannot hide a double count: flipping the meter swaps the two directions
without changing their sum, so a reconstruction that is wrong in total stays
wrong under either sign. On 5 Oct, either sign leaves one direction 19 kWh or
more out.

**A meter with too little history is not evidence.** If more than
`MAX_STALE_PERIODS_PCT` of the day's periods have no fresh reading, the
verdict is `insufficient_coverage`, not agreement. With no meter configured
it is `not_checked`. Neither of those lowers EPR's reliability.
"""

from __future__ import annotations

from collections.abc import Sequence

TOLERANCE_FRACTION = 0.10
TOLERANCE_FLOOR_KWH = 2.0
MAX_STALE_PERIODS_PCT = 10.0

AGREES = "agrees"
DISAGREES = "disagrees"
NOT_CHECKED = "not_checked"
INSUFFICIENT_COVERAGE = "insufficient_coverage"


def metered_energy_kwh(
    metered_kw: Sequence[float], period_hours: Sequence[float]
) -> tuple[float, float]:
    """(positive-direction kWh, negative-direction kWh) of a per-period
    mean power series."""
    pos = neg = 0.0
    for kw, h in zip(metered_kw, period_hours, strict=True):
        if kw > 0:
            pos += kw * h
        else:
            neg -= kw * h
    return pos, neg


def reconcile(
    *,
    meter_sensor: str | None,
    metered_kw: Sequence[float] | None,
    period_hours: Sequence[float] | None,
    stale_periods_pct: float | None,
    reconstructed_import_kwh: float,
    reconstructed_export_kwh: float,
) -> dict[str, object]:
    """The published `meter_reconciliation` record. See the module doc."""
    record: dict[str, object] = {
        "status": NOT_CHECKED,
        "meter_sensor": meter_sensor,
        "reconstructed_import_kwh": round(reconstructed_import_kwh, 3),
        "reconstructed_export_kwh": round(reconstructed_export_kwh, 3),
        "tolerance_fraction": TOLERANCE_FRACTION,
        "tolerance_floor_kwh": TOLERANCE_FLOOR_KWH,
    }
    if not meter_sensor or metered_kw is None or period_hours is None:
        record["reason"] = "no grid meter configured"
        return record
    if stale_periods_pct is None or stale_periods_pct > MAX_STALE_PERIODS_PCT:
        record["status"] = INSUFFICIENT_COVERAGE
        record["stale_periods_pct"] = stale_periods_pct
        record["reason"] = (
            f"the meter has no fresh reading in {stale_periods_pct}% of the day's "
            f"periods (limit {MAX_STALE_PERIODS_PCT}%)"
            if stale_periods_pct is not None
            else "the meter's history could not be measured"
        )
        return record

    pos, neg = metered_energy_kwh(metered_kw, period_hours)
    tolerance = max(TOLERANCE_FLOOR_KWH, TOLERANCE_FRACTION * (pos + neg))
    fits = []
    for sign, imp, exp in ((+1, pos, neg), (-1, neg, pos)):
        r_imp = reconstructed_import_kwh - imp
        r_exp = reconstructed_export_kwh - exp
        fits.append((max(abs(r_imp), abs(r_exp)), sign, imp, exp, r_imp, r_exp))
    fits.sort(key=lambda f: f[0])
    worst, sign, imp, exp, r_imp, r_exp = fits[0]
    record.update(
        {
            "status": AGREES if worst <= tolerance else DISAGREES,
            "meter_sign": "positive_is_import" if sign > 0 else "positive_is_export",
            "metered_import_kwh": round(imp, 3),
            "metered_export_kwh": round(exp, 3),
            "import_residual_kwh": round(r_imp, 3),
            "export_residual_kwh": round(r_exp, 3),
            "tolerance_kwh": round(tolerance, 3),
            "stale_periods_pct": stale_periods_pct,
        }
    )
    if worst > tolerance:
        record["reason"] = (
            f"reconstructed grid differs from the meter by up to {worst:.1f} kWh "
            f"(allowed {tolerance:.1f} kWh)"
        )
    return record
