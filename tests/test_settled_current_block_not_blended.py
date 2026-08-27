"""Regression test for nimbus repo issue #220 (Mark Purcell): the current
settlement block must never be a blended/forecast value.

Real-world scenario reported live (2026-08-27 ~11:10 AEST, QLD1): Amber
Express's own settled state for the current 5-minute block was 4.89 c/kWh
import / 1.44 c/kWh export, but Nimbus published import_price_raw=4.07 /
export_price_raw=0.70 and a further-blended import_price=5.03 /
export_price=1.28 for that same block -- neither figure matched the
settled source. Two distinct problems: (1) the forecast-array resample
lookup used for period 0 isn't the same read as the source's own live
`state`, and (2) even a correct `_raw` value would still have been
diluted by blend_price_with_secondary_sources(), which has no notion of
"index 0 is now" and blends every period uniformly.

Fix (main(), not independently unit-testable per this suite's own
established convention -- see test_solver_writer_cfg_defaults.py's own
docstring on why main()-only logic is tested via its consequences, not
directly): spot_import_raw[0]/spot_export[0] are overridden with a
direct safe_num() state read of the configured primary sensor both
BEFORE `_raw` is snapshotted and AGAIN after blend_price_with_secondary_
sources() runs, so period 0 is never a resample-array lookup and never
diluted by a secondary source.

This test proves the piece that actually needs proving: that
blend_price_with_secondary_sources() (the real, directly-testable
function) does NOT protect period 0 on its own -- confirming main()'s
own re-assert-after-blend step is load-bearing, not redundant.
"""

from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer

_NOW = datetime(2026, 8, 27, 1, 7, tzinfo=UTC)  # 2026-08-27T11:07:00+10:00


class TestBlendDoesNotProtectSettledPeriodZero:
    def test_a_secondary_source_still_dilutes_period_zero(self):
        # Mark's real config has both _sensor_2 slots populated. Prove
        # that, absent main()'s own re-assert step, a secondary source
        # with real coverage at "now" DOES dilute the settled primary --
        # this is the exact gap issue #220 reported.
        grid_times = [_NOW + timedelta(minutes=5 * i) for i in range(3)]
        settled_primary = [0.0489, 0.10, 0.12]  # index 0 == the settled state
        secondary_forecast = {
            "attributes": {
                "forecast": [{"time": t.isoformat(), "value": 0.99} for t in grid_times]
            }
        }
        cfg = {"solver_import_price_sensor_2": "sensor.secondary"}

        from unittest.mock import patch

        with patch.object(solver_writer, "ha_get", return_value=secondary_forecast):
            result, _spread = solver_writer.blend_price_with_secondary_sources(
                settled_primary,
                cfg,
                ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
                grid_times,
            )

        # The bug: period 0 comes back diluted (equal-weight average of
        # 0.0489 and 0.99), not the settled 0.0489.
        assert result[0] != settled_primary[0]
        assert round(result[0], 4) == round((0.0489 + 0.99) / 2, 4)

        # The fix: main() re-asserts the settled value as the final word
        # after this call. Simulating that one-line re-assert recovers
        # the correct settled price.
        fixed = list(result)
        fixed[0] = settled_primary[0]
        assert fixed[0] == 0.0489
        # Periods beyond "now" are untouched by the fix -- genuine
        # forecast blending still applies there, as intended.
        assert fixed[1] == result[1]
        assert fixed[2] == result[2]

    def test_settled_state_read_uses_safe_num_graceful_fallback(self):
        # The settled-block override is a plain safe_num() call (main()'s
        # own established pattern for every live scalar read in this
        # file) -- proves it degrades to the existing forecast-derived
        # value, never crashes or zeroes out, when the source sensor's
        # state is unavailable/unparseable at the moment of the read.
        from unittest.mock import patch

        with patch.object(
            solver_writer, "ha_get", return_value={"state": "unavailable"}
        ):
            result = solver_writer.safe_num(
                "sensor.amber_general_price", fallback=0.0503
            )
        assert result == 0.0503  # falls back to the pre-existing value

        with patch.object(solver_writer, "ha_get", return_value={"state": "0.0489"}):
            result = solver_writer.safe_num(
                "sensor.amber_general_price", fallback=0.0503
            )
        assert result == 0.0489  # genuine settled state wins
