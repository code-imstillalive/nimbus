"""A network's time-of-export reward or charge reaches the export price
(nimbus #2190): in its hours and months only, on the household's own
sources' periods, never on a period a tariff feed already priced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import export_adjustments as ea
import pytest

BNE = timezone(timedelta(hours=10))
ENERGEX_96200X = "11-3 17-20 0.122\n4-10 11-13 -0.0221\n"


def _at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=BNE)


def test_the_reward_and_the_charge_land_in_their_hours_and_seasons_only():
    times = [
        _at("2026-10-31", "11:00"),  # winter midday: the charge
        _at("2026-10-31", "17:00"),  # winter evening: nothing
        _at("2026-11-01", "11:00"),  # summer midday: nothing
        _at("2026-11-01", "16:55"),  # just before the reward
        _at("2026-11-01", "17:00"),  # the reward starts
        _at("2026-11-01", "19:55"),  # still in it
        _at("2026-11-01", "20:00"),  # ended
        _at("2027-01-15", "18:00"),  # the season wraps the year end
        _at("2027-04-01", "18:00"),  # April: no reward
    ]
    base = [0.05] * len(times)
    sources = ["primary"] * len(times)
    out = ea.apply(ENERGEX_96200X, base, sources, times)
    want = [0.05 - 0.0221, 0.05, 0.05, 0.05, 0.172, 0.172, 0.05, 0.172, 0.05]
    assert out == pytest.approx(want)

    # A tariff feed's period carries it already, and a consolidated blend
    # carries part of it: neither gets it twice. The AEMO fallback for a
    # stale retailer carries none, so it does.
    # Plain `fallback` (no source covers the period, so every source's held
    # value is averaged) carries a tariff feed's share too.
    labels = ["primary", "secondary", "consolidated", "aemo_fallback", "fallback"]
    five = [_at("2026-11-01", "17:00")] * 5
    assert ea.apply(ENERGEX_96200X, [0.1] * 5, labels, five) == pytest.approx(
        [0.222, 0.1, 0.1, 0.222, 0.1]
    )

    # Blank or comments only: the price is unchanged.
    assert ea.apply("", base, sources, times) == base
    assert ea.apply("# none\n", base, sources, times) == base
    assert ea.apply(None, base, sources, times) == base
