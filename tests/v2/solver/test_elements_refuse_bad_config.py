"""Every config the solver's elements refuse, refused (#1837).

The coverage-by-risk map put `solver/elements.py` second among tier 1's gaps:
63 statements, nearly all of them a `raise` in a `__post_init__` that no test
reached. Each one keeps a config the LP would read silently wrong (a
negative forecast, a band of another length, half of a pair, a value
function that is not concave) from reaching a plan. Each case below is one
such config, and the message must name what is wrong.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from solver.elements import (
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    DegenerateConfigError,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)

N = 3


def _grid(**kw):
    base = {
        "import_price": np.full(N, 0.30),
        "export_price": np.full(N, 0.10),
        "import_limit_kw": 20.0,
        "export_limit_kw": 10.0,
    }
    return GridConfig(**{**base, **kw})


def _battery(**kw):
    base = {
        "name": "home",
        "capacity_kwh": 10.0,
        "initial_soc_kwh": 5.0,
        "min_soc_kwh": 1.0,
        "max_soc_kwh": 9.0,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    return BatteryConfig(**{**base, **kw})


def _adequacy(**kw):
    base = {
        "name": "pool",
        "max_power_kw": 1.0,
        "target_kwh": 2.0,
        "deadline_period": 2,
        "shortfall_price": 1.0,
    }
    return AdequacyLoadConfig(**{**base, **kw})


def _shed(**kw):
    base = {"name": "spa", "forecast_kw": np.ones(N), "shed_cost": 0.5}
    return SheddableLoadConfig(**{**base, **kw})


REFUSED = [
    # PeriodGrid
    ("no periods", lambda: PeriodGrid(hours=np.array([])), "at least one period"),
    (
        "a zero-length period",
        lambda: PeriodGrid(hours=np.array([1.0, 0.0])),
        "strictly positive",
    ),
    # GridConfig
    (
        "an import limit of another length",
        lambda: _grid(import_limit_kw=np.ones(N + 1)),
        "import_limit_kw",
    ),
    (
        "an export limit of another length",
        lambda: _grid(export_limit_kw=np.ones(N + 1)),
        "export_limit_kw",
    ),
    ("a negative minimum export", lambda: _grid(min_export_kwh=-1.0), "min_export_kwh"),
    (
        "a bonus price with no volume",
        lambda: _grid(export_bonus_price=np.ones(N)),
        "together",
    ),
    (
        "a bonus volume with no price",
        lambda: _grid(export_bonus_volume_kwh=1.0),
        "together",
    ),
    (
        "a negative bonus volume",
        lambda: _grid(export_bonus_price=np.ones(N), export_bonus_volume_kwh=-1.0),
        "export_bonus_volume_kwh",
    ),
    (
        "a bonus price of another length",
        lambda: _grid(export_bonus_price=np.ones(N + 1), export_bonus_volume_kwh=1.0),
        "export_bonus_price",
    ),
    (
        "a fixed export of another length",
        lambda: _grid(fixed_export_kw=np.ones(N + 1)),
        "fixed_export_kw",
    ),
    (
        "an import band of another length",
        lambda: _grid(import_price_upper=np.ones(N + 1)),
        "import_price_upper",
    ),
    (
        "an export band of another length",
        lambda: _grid(export_price_lower=np.ones(N + 1)),
        "export_price_lower",
    ),
    # BatteryConfig
    (
        "a terminal value with a zero-width segment",
        lambda: _battery(terminal_value_breakpoints=[(0.0, 0.2), (8.0, 0.1)]),
        "width",
    ),
    (
        "a terminal value that does not span min to max",
        lambda: _battery(terminal_value_breakpoints=[(4.0, 0.2), (3.0, 0.1)]),
        "sum to exactly",
    ),
    (
        "terminal value indices with no curve",
        lambda: _battery(terminal_value_period_indices=(1,)),
        "terminal_value_breakpoints is None",
    ),
    (
        "a must-have SoC with no period",
        lambda: _battery(must_have_soc_kwh=5.0),
        "together",
    ),
    (
        "a must-have period with no SoC",
        lambda: _battery(must_have_soc_by_period_index=1),
        "together",
    ),
    (
        "a negative must-have period",
        lambda: _battery(must_have_soc_by_period_index=-1, must_have_soc_kwh=5.0),
        ">= 0",
    ),
    (
        "a must-have SoC above capacity",
        lambda: _battery(must_have_soc_by_period_index=1, must_have_soc_kwh=11.0),
        "physical bounds",
    ),
    (
        "a negative shared charger limit",
        lambda: _battery(shared_charger_max_kw=-1.0),
        "cannot be negative",
    ),
    (
        "a power curve with a repeated SoC",
        lambda: _battery(charge_power_curve=[(1.0, 5.0), (1.0, 4.0), (9.0, 1.0)]),
        "same soc_kwh",
    ),
    # Forecasts and their bands
    (
        "a negative solar forecast",
        lambda: SolarConfig(forecast_kw=np.array([0.0, -1.0, 0.0])),
        "negative",
    ),
    (
        "a negative load forecast",
        lambda: LoadConfig(name="house", forecast_kw=np.array([1.0, -1.0, 1.0])),
        "negative",
    ),
    (
        "a load band of another length",
        lambda: LoadConfig(
            name="house",
            forecast_kw=np.ones(N),
            lower_kw=np.ones(N + 1),
            upper_kw=np.ones(N + 1),
        ),
        "periods each",
    ),
    (
        "a negative sheddable forecast",
        lambda: _shed(forecast_kw=-np.ones(N)),
        "negative",
    ),
    ("a sheddable fraction above 1", lambda: _shed(min_fraction=1.5), "min_fraction"),
    ("a free shed", lambda: _shed(shed_cost=0.0), "shed_cost"),
    # AdequacyLoadConfig
    (
        "an adequacy load with no power",
        lambda: _adequacy(max_power_kw=0.0),
        "max_power_kw",
    ),
    (
        "an adequacy load with no target",
        lambda: _adequacy(target_kwh=0.0),
        "target_kwh",
    ),
    (
        "a deadline before the earliest start",
        lambda: _adequacy(earliest_period=2, deadline_period=1),
        "deadline_period",
    ),
    (
        "a negative earliest start",
        lambda: _adequacy(earliest_period=-1),
        "earliest_period",
    ),
    (
        "a negative shortfall price",
        lambda: _adequacy(shortfall_price=-1.0),
        "shortfall_price",
    ),
    (
        "a negative cost cap",
        lambda: _adequacy(max_cost_per_run=-1.0),
        "max_cost_per_run",
    ),
]


def _nan_but(i, value):
    a = np.full(N, np.nan)
    a[i] = value
    return a


def _window(earliest, deadline, target=1.0):
    return AdequacyWindow(
        earliest_period=earliest, deadline_period=deadline, target_kwh=target
    )


# Added on Mark Purcell's review of #2064: every other rejection path in the
# same classes, so the title holds.
REFUSED += [
    # GridConfig: limits, the floor, the pinned export and its band
    ("a negative import limit", lambda: _grid(import_limit_kw=-1.0), "limits"),
    (
        "a negative export limit in one period",
        lambda: _grid(export_limit_kw=np.array([10.0, -1.0, 10.0])),
        "limits",
    ),
    (
        "a negative export floor",
        lambda: _grid(export_floor_price=-0.01),
        "export_floor_price",
    ),
    (
        "a pinned export above the export limit",
        lambda: _grid(fixed_export_kw=_nan_but(1, 11.0)),
        "within [0, export_limit_kw]",
    ),
    (
        "a negative pinned export",
        lambda: _grid(fixed_export_kw=_nan_but(0, -1.0)),
        "within [0, export_limit_kw]",
    ),
    (
        "an export ceiling with no pinned export",
        lambda: _grid(fixed_export_max_kw=np.ones(N)),
        "requires fixed_export_kw",
    ),
    (
        "an export ceiling of another length",
        lambda: _grid(fixed_export_kw=np.ones(N), fixed_export_max_kw=np.ones(N + 1)),
        "fixed_export_max_kw must have the same length",
    ),
    (
        "an export ceiling below the pinned export",
        lambda: _grid(
            fixed_export_kw=np.full(N, 2.0), fixed_export_max_kw=np.full(N, 1.0)
        ),
        "banded entries",
    ),
    (
        "an export ceiling above the export limit",
        lambda: _grid(
            fixed_export_kw=np.full(N, 2.0), fixed_export_max_kw=np.full(N, 11.0)
        ),
        "banded entries",
    ),
    # BatteryConfig: bounds, power, start, index sets, costs, spike override
    (
        "a floor above the ceiling",
        lambda: _battery(min_soc_kwh=9.5),
        "Invalid SoC bounds",
    ),
    (
        "a ceiling above capacity",
        lambda: _battery(max_soc_kwh=11.0),
        "Invalid SoC bounds",
    ),
    ("a negative floor", lambda: _battery(min_soc_kwh=-1.0), "Invalid SoC bounds"),
    (
        "no capacity",
        lambda: _battery(
            capacity_kwh=0.0, min_soc_kwh=0.0, max_soc_kwh=0.0, initial_soc_kwh=0.0
        ),
        "capacity_kwh",
    ),
    ("a negative charge rate", lambda: _battery(max_charge_kw=-1.0), "max_charge_kw"),
    (
        "a negative discharge rate",
        lambda: _battery(max_discharge_kw=-1.0),
        "max_discharge_kw",
    ),
    (
        "a start above capacity",
        lambda: _battery(initial_soc_kwh=11.0),
        "initial_soc_kwh",
    ),
    ("a negative start", lambda: _battery(initial_soc_kwh=-1.0), "initial_soc_kwh"),
    (
        "a negative unavailable period",
        lambda: _battery(unavailable_period_indices=[-1]),
        "unavailable_period_indices",
    ),
    (
        "a negative terminal value period",
        lambda: _battery(
            terminal_value_breakpoints=[(8.0, 0.1)], terminal_value_period_indices=(-1,)
        ),
        "non-negative",
    ),
    (
        "a terminal value period twice",
        lambda: _battery(
            terminal_value_breakpoints=[(8.0, 0.1)],
            terminal_value_period_indices=(1, 1),
        ),
        "duplicates",
    ),
    (
        "a negative degradation cost",
        lambda: _battery(degradation_cost_per_kwh=-0.01),
        "degradation_cost_per_kwh",
    ),
    (
        "a spike override above the discharge rate",
        lambda: _battery(spike_override_discharge_kw=6.0),
        "spike_override_discharge_kw",
    ),
    (
        "a negative spike override",
        lambda: _battery(spike_override_discharge_kw=-1.0),
        "spike_override_discharge_kw",
    ),
    # Power curves (the battery's floor is 1, its ceiling 9)
    (
        "a power curve of one point",
        lambda: _battery(charge_power_curve=[(1.0, 5.0)]),
        "at least 2 points",
    ),
    (
        "a power curve out of order",
        lambda: _battery(charge_power_curve=[(9.0, 1.0), (1.0, 5.0)]),
        "ascending",
    ),
    (
        "a power curve that starts above the floor",
        lambda: _battery(charge_power_curve=[(2.0, 5.0), (9.0, 1.0)]),
        "first point",
    ),
    (
        "a power curve that stops below the ceiling",
        lambda: _battery(charge_power_curve=[(1.0, 5.0), (8.0, 1.0)]),
        "last point",
    ),
    (
        "a power curve with a negative power",
        lambda: _battery(charge_power_curve=[(1.0, 5.0), (9.0, -1.0)]),
        "cannot be negative",
    ),
    (
        "a discharge curve of one point",
        lambda: _battery(discharge_power_curve=[(1.0, 5.0)]),
        "discharge_power_curve",
    ),
    # Confidence bands: one-sided, and not around the forecast
    (
        "a solar band with no upper side",
        lambda: SolarConfig(forecast_kw=np.ones(N), lower_kw=np.zeros(N)),
        "both be given",
    ),
    (
        "a solar band above the forecast",
        lambda: SolarConfig(
            forecast_kw=np.ones(N), lower_kw=np.full(N, 2.0), upper_kw=np.full(N, 3.0)
        ),
        "lower_kw <= forecast_kw <= upper_kw",
    ),
    (
        "a solar quantile band with no p90",
        lambda: SolarConfig(forecast_kw=np.ones(N), p10_kw=np.zeros(N)),
        "Solar quantile",
    ),
    (
        "a load band with no lower side",
        lambda: LoadConfig(name="house", forecast_kw=np.ones(N), upper_kw=np.ones(N)),
        "both be given",
    ),
    (
        "a load band below the forecast",
        lambda: LoadConfig(
            name="house",
            forecast_kw=np.ones(N),
            lower_kw=np.zeros(N),
            upper_kw=np.full(N, 0.5),
        ),
        "lower_kw <= forecast_kw <= upper_kw",
    ),
    (
        "a sheddable band with no upper side",
        lambda: _shed(lower_kw=np.zeros(N)),
        "both be given",
    ),
    (
        "a sheddable band above the forecast",
        lambda: _shed(lower_kw=np.full(N, 2.0), upper_kw=np.full(N, 3.0)),
        "lower_kw <= forecast_kw <= upper_kw",
    ),
    # AdequacyLoadConfig: daily cap, deferral saving, windows
    (
        "a daily cap of zero",
        lambda: _adequacy(max_kwh_per_day=0.0),
        "max_kwh_per_day",
    ),
    (
        "a negative deferral saving",
        lambda: _adequacy(min_deferral_saving_dollars=-0.01),
        "min_deferral_saving_dollars",
    ),
    ("an empty window list", lambda: _adequacy(windows=()), "non-empty"),
    (
        "windows and allowed together",
        lambda: _adequacy(windows=(_window(0, 1),), allowed=np.ones(N, dtype=bool)),
        "both windows and",
    ),
    (
        "a window with a negative start",
        lambda: _adequacy(windows=(_window(-1, 1),)),
        "windows[0].earliest_period",
    ),
    (
        "a window with no target",
        lambda: _adequacy(windows=(_window(0, 1, target=0.0),)),
        "windows[0].target_kwh",
    ),
    (
        "a window that ends before it starts",
        lambda: _adequacy(windows=(_window(2, 1),)),
        "windows[0].deadline_period",
    ),
    (
        "windows that overlap",
        lambda: _adequacy(windows=(_window(0, 1), _window(1, 2))),
        "chronologically",
    ),
]


@pytest.mark.parametrize(
    ("case", "build", "says"), REFUSED, ids=[c for c, _b, _s in REFUSED]
)
def test_a_config_the_lp_would_read_wrong_is_refused(case, build, says):
    with pytest.raises(ValueError) as err:
        build()
    assert says in str(err.value), f"{case}: {err.value}"


DEGENERATE = [
    (
        "charge plus discharge cost below the floor",
        lambda: _battery(charge_cost=0.0, discharge_cost=0.0),
    ),
    (
        "a terminal value whose rate rises",
        lambda: _battery(terminal_value_breakpoints=[(4.0, 0.1), (4.0, 0.2)]),
    ),
    ("a lossless charge", lambda: _battery(charge_efficiency=1.0)),
    ("a discharge with no output", lambda: _battery(discharge_efficiency=0.0)),
    (
        "a power curve that bends upward",
        lambda: _battery(charge_power_curve=[(1.0, 1.0), (5.0, 1.5), (9.0, 5.0)]),
    ),
]


@pytest.mark.parametrize(("case", "build"), DEGENERATE, ids=[c for c, _b in DEGENERATE])
def test_a_config_the_lp_could_exploit_is_refused_as_degenerate(case, build):
    # DegenerateConfigError, not only a ValueError: the config is well formed
    # but hands the LP a free trade (a wash trade, or skipping to a richer
    # segment) the plan would take.
    with pytest.raises(DegenerateConfigError):
        build()


def test_index_sets_given_as_lists_are_kept_as_frozensets():
    b = _battery(charge_by_period_indices=[2, 1], stale_history_period_indices=[0])
    assert b.charge_by_period_indices == frozenset({1, 2})
    assert b.stale_history_period_indices == frozenset({0})
    assert isinstance(dataclasses.replace(b).charge_by_period_indices, frozenset)
