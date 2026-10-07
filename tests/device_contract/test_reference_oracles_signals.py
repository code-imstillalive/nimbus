"""Spec DC-000 F04, F09: signal reference oracles, read from each fixture's
manifest and independent of production code."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.device_contract.oracles_signals import (
    PriceInterval,
    Row,
    UnitError,
    counter_delta,
    declared_rating_kw,
    power_kw,
    price_over,
    price_with_fees,
    reading,
)

TOL = 1e-9
FIX = Path(__file__).parent / "fixtures"
F04 = json.loads(
    (FIX / "F04_price_intervals" / "manifest.json").read_text(encoding="utf-8")
)
F09 = json.loads(
    (FIX / "F09_units_and_samples" / "manifest.json").read_text(encoding="utf-8")
)
T = datetime.fromisoformat


def _intervals(raw: list) -> list[PriceInterval]:
    return [PriceInterval(T(a), T(b), p) for a, b, p in raw]


def _rows(raw: list) -> list[Row]:
    return [Row(T(at), state) for at, state in raw]


# F04: prices on their published intervals ------------------------------


@pytest.mark.parametrize(
    "name", ["thirty_min_window_over_5_min", "gap", "nothing_published"]
)
def test_f04_price_over_a_window(name: str) -> None:
    case, expected = F04["cases"][name], F04["expected"][name]
    price, coverage = price_over(_intervals(case["intervals"]), *map(T, case["window"]))
    assert abs(coverage - expected["coverage"]) <= TOL
    if expected["price"] is None:
        assert price is None
    else:
        assert price is not None and abs(price - expected["price"]) <= TOL


def test_f04_a_utc_interval_is_the_same_instant_and_its_negative_price_is_kept() -> (
    None
):
    raw = F04["cases"]["thirty_min_window_over_5_min"]["intervals"]
    utc = _intervals(raw)[2]
    assert utc.start == T("2026-10-05T17:10:00+10:00")
    price, _ = price_over([utc], utc.start, utc.end)
    assert price == -0.06


def test_f04_overlapping_published_intervals_are_refused() -> None:
    a = PriceInterval(
        T("2026-10-05T17:00:00+10:00"), T("2026-10-05T17:30:00+10:00"), 0.1
    )
    b = PriceInterval(
        T("2026-10-05T17:15:00+10:00"), T("2026-10-05T17:45:00+10:00"), 0.2
    )
    with pytest.raises(ValueError, match="overlapping"):
        price_over([a, b], a.start, b.end)


def test_f04_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        price_over([], datetime(2026, 10, 5, 17), datetime(2026, 10, 5, 18))  # noqa: DTZ001


def test_f04_fees_are_added_once_and_a_missing_direction_stays_missing() -> None:
    case, expected = F04["cases"]["fees"], F04["expected"]["fees"]
    assert (
        abs(
            price_with_fees(case["spot"], case["fees"], False)
            - expected["fees_not_included"]
        )
        <= TOL
    )
    assert (
        price_with_fees(case["spot"], case["fees"], True) == expected["fees_included"]
    )
    assert price_with_fees(None, case["fees"], False) is expected["missing_direction"]


# F09: units, samples, counters -----------------------------------------


@pytest.mark.parametrize(("value", "unit", "kw"), F09["cases"]["units"])
def test_f09_power_units(value: float, unit: str, kw: float) -> None:
    got = power_kw(value, unit)
    assert got is not None and abs(got - kw) <= TOL


@pytest.mark.parametrize("unit", F09["cases"]["refused_units"])
def test_f09_energy_and_miscased_units_are_refused(unit: str) -> None:
    with pytest.raises(UnitError):
        power_kw(1.0, unit)


@pytest.mark.parametrize(("state", "value"), F09["cases"]["states"])
def test_f09_missing_is_distinct_from_zero(state: str, value: str | None) -> None:
    got = reading(state)
    assert got is None if value is None else got == float(value)


def test_f09_a_held_counter_is_measured_unchanged() -> None:
    case = F09["cases"]["held_counter"]
    assert (
        counter_delta(_rows(case["rows"]), *map(T, case["window"]))
        == F09["expected"]["held_counter_kwh"]
    )


def test_f09_a_reading_at_the_interval_end_is_counted_once() -> None:
    rows = _rows(F09["cases"]["edge_reading"]["rows"])
    first = counter_delta(
        rows, T("2026-10-05T09:00:00+10:00"), T("2026-10-05T10:00:00+10:00")
    )
    second = counter_delta(
        rows, T("2026-10-05T10:00:00+10:00"), T("2026-10-05T11:00:00+10:00")
    )
    expected = F09["expected"]["edge_reading_kwh"]
    assert first is not None and abs(first - expected["09-10"]) <= TOL
    assert second is not None and abs(second - expected["10-11"]) <= TOL


def test_f09_a_reset_is_new_energy_not_a_negative_delta() -> None:
    case = F09["cases"]["reset"]
    got = counter_delta(_rows(case["rows"]), *map(T, case["window"]))
    assert got is not None and abs(got - F09["expected"]["reset_kwh"]) <= TOL


def test_f09_a_gap_inside_the_window_leaves_it_unresolved() -> None:
    case = F09["cases"]["gap_in_window"]
    assert counter_delta(_rows(case["rows"]), *map(T, case["window"])) is None


def test_f09_no_rating_is_inferred_from_a_historical_peak() -> None:
    case = F09["cases"]["rating"]
    assert (
        declared_rating_kw(case["declared"], case["history_kw"])
        is F09["expected"]["rating_kw"]
    )
    assert declared_rating_kw(5.0, case["history_kw"]) == 5.0
