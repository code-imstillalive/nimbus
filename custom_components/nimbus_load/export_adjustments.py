"""Network time-of-export adjustments on the export price (nimbus #2190).

Some network tariffs pay a reward, or levy a charge, per exported kWh in set
hours and months (Energex's two-way tariff 96200X: +12 c/kWh 17:00-20:00
November to March, -2 c/kWh 11:00-13:00 April to October). A retailer's own
sell price may leave it out (LocalVolts' Flex Up does), while a tariff feed
such as NEM PD7DAY's export tariff already includes it.

The household sets the adjustments as text, one per line:

    <first month>-<last month> <start hour>-<end hour> <$/kWh>

for example `11-3 17-20 0.122` (a month range may wrap the year end; the
hours cover start:00 up to, not including, end:00; the amount is signed, a
charge negative). Each is added to the export price of every period priced
from the household's own sources, and never to a period a secondary source
(a tariff feed) filled, which carries it already.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple


class ExportAdjustment(NamedTuple):
    first_month: int
    last_month: int
    start_hour: int
    end_hour: int
    rate: float

    def applies(self, local: datetime) -> bool:
        m = local.month
        if self.first_month <= self.last_month:
            in_months = self.first_month <= m <= self.last_month
        else:  # wraps the year end, e.g. 11-3
            in_months = m >= self.first_month or m <= self.last_month
        return in_months and self.start_hour <= local.hour < self.end_hour


def parse(text: str | None) -> tuple[list[ExportAdjustment], list[str]]:
    """The adjustments in `text`, and a message per line that isn't one."""
    out: list[ExportAdjustment] = []
    problems: list[str] = []
    for n, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            months, hours, rate = line.split()
            m1, m2 = (int(x) for x in months.split("-"))
            h1, h2 = (int(x) for x in hours.split("-"))
            value = float(rate)
        except ValueError:
            problems.append(
                f"line {n}: expected 'months hours $/kWh', e.g. 11-3 17-20 0.122"
            )
            continue
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
            problems.append(f"line {n}: months are 1 to 12")
        elif not (0 <= h1 < h2 <= 24):
            problems.append(f"line {n}: hours are 0 to 24, start before end")
        elif not -2.0 <= value <= 2.0:
            problems.append(f"line {n}: the amount is in $/kWh, between -2 and 2")
        else:
            out.append(ExportAdjustment(m1, m2, h1, h2, value))
    return out, problems


def apply(
    text: str | None,
    export_price: Sequence[float],
    sources: Sequence[str],
    local_times: Sequence[datetime],
) -> list[float]:
    """`export_price` with each period's adjustments added, except where
    `sources` says a secondary source (a tariff feed) priced it."""
    adjustments, _problems = parse(text)
    prices = list(export_price)
    if not adjustments:
        return prices
    for i, local in enumerate(local_times[: len(prices)]):
        if i < len(sources) and sources[i] == "secondary":
            continue
        prices[i] += sum(a.rate for a in adjustments if a.applies(local))
    return prices
