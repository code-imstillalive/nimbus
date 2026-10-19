"""A site's loss factors, looked up from AEMO's published tables (#1786).

A LocalVolts site is paid the regional spot price adjusted for losses:
spot = RRP x MLF x DLF, and the import side adds GST. Mark measured it to
within 0.1 % on two sites (NSW: 0.9912 x 1.0594 = 1.05008; QLD: 1.0605). The
factors are not fitted: the site's transmission node (TNI) and distribution
loss factor code identify them, and AEMO publishes their values for each
financial year. Both codes come from the retailer (LV v2 2.13.0 exposes them
as `transmission_node_identifier` and `distribution_loss_factor_code`).

`data/loss_factors.json` holds AEMO's 2025-26 and 2026-27 tables. A code that
AEMO lists with more than one value is stored as null and never guessed. The
financial year runs 1 July to 30 June in NEM time (UTC+10, no daylight
saving), so a plan that crosses 1 July prices each interval with its own
year's factors.

Pure: no Home Assistant import.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import NamedTuple

NEM_TIME = timezone(timedelta(hours=10))
# Import carries GST on top of the loss-adjusted spot; export does not. Mark
# measured LocalVolts' buy spot = sell spot x 1.1000 on all 287 intervals
# (#1786). Australia's GST, so a fact of the NEM rather than a site setting.
IMPORT_GST = 1.10
_TABLE = Path(__file__).resolve().parent.parent / "data" / "loss_factors.json"


class SiteFactor(NamedTuple):
    """`value` is MLF x DLF, or None with `reason` saying why."""

    value: float | None
    reason: str  # ok | no_codes | year_missing | unknown_tni | unknown_dlf | ambiguous


@cache
def _table() -> dict:
    with _TABLE.open(encoding="utf-8") as f:
        return json.load(f)


def financial_year(when: datetime) -> str:
    """'2026-27' for 1 July 2026 to 30 June 2027, in NEM time."""
    t = when.astimezone(NEM_TIME) if when.tzinfo else when
    start = t.year if t.month >= 7 else t.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def years() -> list[str]:
    """The financial years the table covers."""
    return sorted(set(_table()["mlf"]) & set(_table()["dlf"]))


def _clean(code: str | None) -> str | None:
    if code is None:
        return None
    code = str(code).strip().upper()
    return code or None


def site_factor(tni: str | None, dlf_code: str | None, when: datetime) -> SiteFactor:
    """MLF(tni) x DLF(dlf_code) for the financial year containing `when`."""
    tni, dlf_code = _clean(tni), _clean(dlf_code)
    if tni is None or dlf_code is None:
        return SiteFactor(None, "no_codes")
    fy = financial_year(when)
    mlf_year = _table()["mlf"].get(fy)
    dlf_year = _table()["dlf"].get(fy)
    if mlf_year is None or dlf_year is None:
        return SiteFactor(None, "year_missing")
    if tni not in mlf_year:
        return SiteFactor(None, "unknown_tni")
    if dlf_code not in dlf_year:
        return SiteFactor(None, "unknown_dlf")
    mlf, dlf = mlf_year[tni], dlf_year[dlf_code]
    if mlf is None or dlf is None:
        return SiteFactor(None, "ambiguous")
    return SiteFactor(mlf * dlf, "ok")
