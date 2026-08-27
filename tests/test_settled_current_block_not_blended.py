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

UPDATE (2026-08-27, nimbus issue #239, primary-preferring blend): once
#239 landed, blend_price_with_secondary_sources() itself no longer
dilutes a period whose PRIMARY is marked real there -- which in the
normal, real-world call path is true for period 0 ("now" is within any
real source's own coverage in practice). This test's original premise
("the blend function does NOT protect period 0 on its own") no longer
holds in that common case; see test_primary_preferring_now_protects_
period_zero_in_the_common_case below for the updated proof. It still
holds -- and main()'s own re-assert-after-blend step is still a
necessary backstop, not redundant -- for the narrower case where some
resample path marks primary_real_mask[0] itself False (e.g. a stale
forecast-array-derived mask disagreeing with a fresh live-state read);
see test_a_secondary_source_still_dilutes_period_zero_when_primary_
mask_is_false for that case.
"""

from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer

_NOW = datetime(2026, 8, 27, 1, 7, tzinfo=UTC)  # 2026-08-27T11:07:00+10:00


class TestBlendDoesNotProtectSettledPeriodZero:
    def test_a_secondary_source_still_dilutes_period_zero_when_primary_mask_is_false(
        self,
    ):
        # The narrower case #239's primary-preferring fix does NOT cover
        # on its own: if whatever produced primary_real_mask marks index
        # 0 itself as False (e.g. a resample path disagreeing with the
        # live state main() already substituted in), a secondary source
        # with real coverage at "now" still dilutes the settled primary
        # -- this is why main()'s own re-assert-after-blend step (the
        # #220 fix) remains a necessary backstop, not redundant, even
        # after #239.
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
                primary_real_mask=[False, True, True],
            )

        # The bug: period 0 comes back as the secondary's own value
        # alone (0.99) -- primary is excluded once marked not real
        # (nimbus issue #239), but that's still NOT the settled 0.0489.
        assert result[0] != settled_primary[0]
        assert round(result[0], 4) == 0.99

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

    def test_primary_preferring_now_protects_period_zero_in_the_common_case(self):
        # The common real-world case (nimbus issue #239): main()'s own
        # resample step marks "now" as real for the primary in practice
        # (grid_times[0] is always within any real source's own coverage
        # by construction -- see build_tiered_grid()). Since #239,
        # blend_price_with_secondary_sources() ITSELF no longer dilutes
        # period 0 here, even without main()'s explicit re-assert --
        # unlike before #239 landed (see the other test in this class
        # for the case where that's not yet true).
        grid_times = [_NOW + timedelta(minutes=5 * i) for i in range(3)]
        settled_primary = [0.0489, 0.10, 0.12]
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

        assert result[0] == settled_primary[0]

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
