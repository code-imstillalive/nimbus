"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1291): `8704f2b` ("Part of issue 1179: carry the blend weight
actually used onto LPResult") added `calibration_weight_used` to `LPResult`
and threaded it correctly through `solver/lp.py` -- but never carried it the
rest of the way.

`solver/network.py`'s two `Plan`-construction sites that copy
`calibration_min_weight_fallback`/`calibration_fallback_reason` from
`LPResult` onto `Plan` (~line 3817-3818 and 4355-4356) do NOT also copy
`calibration_weight_used`. Grep-confirmed: `calibration_weight_used`
appears nowhere outside `solver/lp.py` in the whole package. The commit's
own stated purpose -- "carrying the blend weight actually used into the
failure warning... becomes a field you read rather than an alignment you
infer" -- is not achieved, because the value dead-ends inside `LPResult`
and never reaches `Plan`, the failure-warning log line, or any published
sensor.

This mirrors `TestEveryConstructionSiteCarriesIt` in
`tests/test_1179_the_blend_weight_reaches_the_result.py` (which pins the
*first* half of the trip, LPResult's own construction sites) one hop
further down the pipe: every `Plan` construction that carries the sibling
`calibration_fallback_reason` field must also carry `calibration_weight_used`,
the same "a construction carrying the flag but not the weight is a
half-propagated change" reasoning that test already uses for `LPResult`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import network as network_mod


class TestPlanCarriesTheField(unittest.TestCase):
    @pytest.mark.xfail(
        reason="nimbus #1291: calibration_weight_used was never added to Plan, "
        "only to LPResult -- it dead-ends and never reaches the failure warning",
        strict=True,
    )
    def test_plan_dataclass_has_the_field(self):
        self.assertIn(
            "calibration_weight_used",
            network_mod.Plan.__dataclass_fields__,
            "Plan must carry calibration_weight_used, the same as its sibling "
            "calibration_fallback_reason, so a failed solve's blend weight "
            "actually reaches something a household or log line can read",
        )


class TestEveryPlanConstructionSiteCarriesIt(unittest.TestCase):
    @pytest.mark.xfail(
        reason="nimbus #1291: no Plan(...) construction copies "
        "calibration_weight_used from LPResult, even where it copies the "
        "sibling calibration_fallback_reason field",
        strict=True,
    )
    def test_no_Plan_construction_carries_the_reason_without_the_weight(self):
        """Mirrors test_1179_the_blend_weight_reaches_the_result.py's own
        TestEveryConstructionSiteCarriesIt, one hop further down the pipe:
        LPResult -> Plan instead of the calibrator -> LPResult."""
        src = Path(network_mod.__file__).read_text(encoding="utf-8")
        reason = "calibration_fallback_reason=result.calibration_fallback_reason"
        weight = "calibration_weight_used=result.calibration_weight_used"
        self.assertEqual(
            src.count(reason),
            src.count(weight),
            "every Plan(...) construction carrying the fallback reason from "
            "result must also carry the weight (nimbus #1291)",
        )


if __name__ == "__main__":
    unittest.main()
