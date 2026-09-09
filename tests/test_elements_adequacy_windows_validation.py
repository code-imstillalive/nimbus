"""nimbus issue #612: validation tests for AdequacyLoadConfig.windows /
AdequacyWindow -- the repeating-window path that lets a deferrable load
owe target_kwh fresh every calendar day within the horizon, instead of
once wherever the single earliest/deadline window happens to land.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import AdequacyLoadConfig, AdequacyWindow


def _base_kwargs(**overrides) -> dict:
    defaults = {
        "name": "hws",
        "max_power_kw": 3.7,
        "target_kwh": 5.0,
        "deadline_period": 10,
        "shortfall_price": 10.0,
    }
    defaults.update(overrides)
    return defaults


class TestAdequacyWindowsValidation(unittest.TestCase):
    def test_a_valid_multi_window_config_constructs_fine(self):
        cfg = AdequacyLoadConfig(
            **_base_kwargs(
                windows=(
                    AdequacyWindow(
                        earliest_period=6, deadline_period=16, target_kwh=2.0
                    ),
                    AdequacyWindow(
                        earliest_period=30, deadline_period=40, target_kwh=2.0
                    ),
                )
            )
        )
        self.assertEqual(len(cfg.windows), 2)

    def test_empty_windows_tuple_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            AdequacyLoadConfig(**_base_kwargs(windows=()))

    def test_windows_and_allowed_together_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "both windows and allowed"):
            AdequacyLoadConfig(
                **_base_kwargs(
                    windows=(
                        AdequacyWindow(
                            earliest_period=0, deadline_period=5, target_kwh=1.0
                        ),
                    ),
                    allowed=np.ones(11, dtype=bool),
                )
            )

    def test_a_windows_own_deadline_before_its_own_earliest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be >= earliest_period"):
            AdequacyLoadConfig(
                **_base_kwargs(
                    windows=(
                        AdequacyWindow(
                            earliest_period=10, deadline_period=5, target_kwh=1.0
                        ),
                    )
                )
            )

    def test_a_windows_own_negative_target_kwh_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "target_kwh must be > 0"):
            AdequacyLoadConfig(
                **_base_kwargs(
                    windows=(
                        AdequacyWindow(
                            earliest_period=0, deadline_period=5, target_kwh=0.0
                        ),
                    )
                )
            )

    def test_a_windows_own_negative_earliest_period_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "earliest_period must be >= 0"):
            AdequacyLoadConfig(
                **_base_kwargs(
                    windows=(
                        AdequacyWindow(
                            earliest_period=-1, deadline_period=5, target_kwh=1.0
                        ),
                    )
                )
            )

    def test_overlapping_windows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-overlapping"):
            AdequacyLoadConfig(
                **_base_kwargs(
                    windows=(
                        AdequacyWindow(
                            earliest_period=0, deadline_period=10, target_kwh=1.0
                        ),
                        AdequacyWindow(
                            earliest_period=10, deadline_period=20, target_kwh=1.0
                        ),
                    )
                )
            )

    def test_out_of_order_windows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-overlapping"):
            AdequacyLoadConfig(
                **_base_kwargs(
                    windows=(
                        AdequacyWindow(
                            earliest_period=30, deadline_period=40, target_kwh=1.0
                        ),
                        AdequacyWindow(
                            earliest_period=6, deadline_period=16, target_kwh=1.0
                        ),
                    )
                )
            )

    def test_windows_none_is_a_complete_no_op(self):
        # Every existing single-window caller/test keeps working
        # unchanged -- windows defaults to None.
        cfg = AdequacyLoadConfig(**_base_kwargs())
        self.assertIsNone(cfg.windows)


if __name__ == "__main__":
    unittest.main()
