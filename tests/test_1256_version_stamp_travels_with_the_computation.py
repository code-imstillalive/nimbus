"""nimbus issue #1256: the headline `nimbus_version` must describe the release
that COMPUTED the published figures, not the release that happens to be running.

## Measured on production, 2026-09-26

Production ran v0.94.417 until a deploy + container restart at **10:38 AEST**,
then v0.94.420. Read after that restart:

| sensor | `generated_at` | `nimbus_version` |
|---|---|---|
| `sensor.nimbus_efficiency_backtest` | 2026-09-26T00:00:00 | **0.94.420** |
| `sensor.nimbus_solver_quality_report` | 2026-09-26T06:06:00 | **0.94.420** |
| `sensor.nimbus_counterfactual_soc` | 2026-09-26T00:00:00 | **0.94.420** |
| `sensor.nimbus_flex_report` | 2026-09-26T00:00:13 | **0.94.420** |

Every `generated_at` precedes the deploy, so every figure was computed by
v0.94.417. All four were labelled v0.94.420. The quality report had reported
`0.94.417` correctly a few hours earlier, so the stamp moved across the restart
while the figures did not.

## The mechanism, traced

Two designs collided on one attribute name:

* **#972** put `nimbus_version` on the *entity*, as a fallback in
  `extra_state_attributes`, meaning "which install does this entity belong
  to". It is deliberately the RUNNING version -- that is what makes it useful
  for mirror detection.
* **#1120/#1219** put `nimbus_version` on a *history row*, meaning "which
  release computed these figures".

`_async_restore_last_value()` then made the collision concrete: it **dropped**
`nimbus_version` from the restored attributes, documented as *"it describes the
running install, not the stored value, and #972 exists precisely so it can be
trusted"*. So the restored figures arrived unstamped and the property re-injected
the new running version onto them -- the exact lie that rule was written to
prevent, arriving by the mechanism meant to prevent it.

## Why exactly four sensors

`_async_restore_last_value()` only runs for classes with
`_RESTORE_ACROSS_RESTART = True`, and that is precisely the four above. Every
other push sensor recomputes within a minute of a restart, so it has no stale
figures for a stamp to misdescribe. The defect is structurally confined to the
once-a-day reports, which is why the measured table is not a sample of a wider
problem -- it is the whole of it.

## Why it is not cosmetic

It misled during the exact investigation the stamp was added for. A published
backtest showing strictly monotonic candidate costs -- the circularity #1232 is
about, fixed by #1233 in v0.94.420 -- stamped `.420` reads as "the fix shipped
and did not work". It was a `.417` computation wearing a `.420` label. This
project's own standing lesson is that *a version difference is not an
explanation for a wrong number until the diff is actually read*; a stamp that
lies makes that check harder, not easier.

## What these tests pin

* Each of the four daily publishers stamps the version **in the attributes it
  posts**, so the claim travels with the computation the way `generated_at`
  already does.
* An unknown version publishes **no key at all**, not `None` -- because #972's
  fallback declines to overwrite a key that is already present, so a `None`
  would publish through and suppress the one answer that would have been true.
* The restore keeps the stamp, and HA-managed metadata is still dropped.
* After a restore, the entity reports the **stored** version, not the running
  one -- the end-to-end shape of the production failure.
* #972's fallback still fires for a publisher that does not stamp, so mirror
  detection is not traded away for this.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer  # noqa: E402


class TestTheStampIsAValueNotAPlaceholder(unittest.TestCase):
    """`_version_stamp()` returns a dict to splat, for one specific reason."""

    def test_a_known_version_is_stamped(self):
        with mock.patch.object(
            solver_writer, "_nimbus_version", return_value="0.94.417"
        ):
            self.assertEqual(
                solver_writer._version_stamp(), {"nimbus_version": "0.94.417"}
            )

    def test_an_unknown_version_stamps_NOTHING_rather_than_None(self):
        """An explicit `"nimbus_version": None` is worse than an absent key.

        The entity layer's #972 fallback declines to overwrite a key that is
        already present, so a None would publish through as None AND suppress
        the fallback that would otherwise have said something true. An absent
        key reads exactly as every row written before #1120 did.
        """
        with mock.patch.object(solver_writer, "_nimbus_version", return_value=None):
            self.assertEqual(solver_writer._version_stamp(), {})


class _PublisherCase(unittest.TestCase):
    """Capture whatever a publisher posts, without an HA instance."""

    def _capture(self, publish, **patches):
        posted: dict = {}

        def post(entity_id, state, attributes):
            posted["entity_id"] = entity_id
            posted["state"] = state
            posted["attrs"] = attributes

        def unreachable(*_a, **_k):
            # Force the compute path rather than the idempotency fast path:
            # every one of these publishers reads its own currently-published
            # state first, and a miss is what a first publish looks like.
            raise solver_writer.urllib.error.URLError("no HA in this test")

        stack = [
            mock.patch.object(solver_writer, "ha_post_state", side_effect=post),
            mock.patch.object(solver_writer, "ha_get", side_effect=unreachable),
            mock.patch.object(
                solver_writer, "_nimbus_version", return_value="0.94.417"
            ),
        ]
        for target, kwargs in patches.items():
            stack.append(mock.patch.object(solver_writer, target, **kwargs))
        for patcher in stack:
            patcher.start()
        try:
            publish()
        finally:
            for patcher in reversed(stack):
                patcher.stop()
        return posted


class TestEachDailyPublisherStampsTheComputation(_PublisherCase):
    """The four sensors #1256 measured, and the four with
    `_RESTORE_ACROSS_RESTART = True` -- the same four, which is why this list
    is complete rather than a sample."""

    _NOW = None  # set per test

    def _now(self):
        from datetime import datetime

        return datetime(2026, 9, 26, 6, 6, tzinfo=solver_writer.LOCAL_TZ)

    def test_the_quality_report_stamps_it(self):
        posted = self._capture(
            lambda: solver_writer.publish_daily_quality_report({}, self._now()),
            compute_daily_quality_report={
                "return_value": {
                    "epr": 0.8185,
                    "epr_pct": 81.85,
                    "j_ref": 1.0,
                    "j_ach": -1.0,
                    "j_star": -2.0,
                    "regret_dollars": 1.0,
                }
            },
        )
        self.assertEqual(posted["attrs"]["nimbus_version"], "0.94.417")
        self.assertIn("generated_at", posted["attrs"])

    def test_the_efficiency_backtest_stamps_it(self):
        posted = self._capture(
            lambda: solver_writer.publish_efficiency_backtest_report({}, self._now()),
            compute_efficiency_backtest_report={
                "return_value": {
                    "spread_dollars": 1.4686,
                    "best_candidate": "99%",
                    "worst_candidate": "85%",
                }
            },
        )
        self.assertEqual(posted["attrs"]["nimbus_version"], "0.94.417")

    def test_the_counterfactual_stamps_it(self):
        posted = self._capture(
            lambda: solver_writer.publish_nimbus_only_soc_counterfactual(
                {}, self._now()
            ),
            compute_nimbus_only_soc_counterfactual={
                "return_value": {"nimbus_only_soc_close_pct": 61.2}
            },
        )
        self.assertEqual(posted["attrs"]["nimbus_version"], "0.94.417")

    def test_the_flex_report_stamps_it(self):
        posted = self._capture(
            lambda: solver_writer.publish_daily_flex_report({}, self._now()),
            compute_daily_flex_report={
                "return_value": {
                    "realised_up_kwh": 1.0,
                    "realised_down_kwh": 2.0,
                }
            },
        )
        self.assertEqual(posted["attrs"]["nimbus_version"], "0.94.417")


class TestEveryRestoringPublisherIsCovered(unittest.TestCase):
    """The guard that keeps this fix complete as sensors are added.

    If a fifth sensor ever sets `_RESTORE_ACROSS_RESTART = True`, it inherits
    #1256's defect silently -- its figures survive a restart and its stamp does
    not. This fails the moment that happens, naming the class.
    """

    def test_the_restoring_classes_are_exactly_the_four_that_stamp(self):
        import inspect

        from custom_components.nimbus_load import sensor as sensor_module

        restoring = sorted(
            name
            for name, obj in vars(sensor_module).items()
            if inspect.isclass(obj)
            and getattr(obj, "_RESTORE_ACROSS_RESTART", False) is True
        )
        self.assertEqual(
            restoring,
            [
                "NimbusCounterfactualSocSensor",
                "NimbusEfficiencyBacktestSensor",
                "NimbusFlexReportSensor",
                "NimbusSolverQualityReportSensor",
            ],
            "a new sensor that restores across a restart must also have its "
            "publisher stamp nimbus_version with the computation (see "
            "solver_writer._version_stamp()), or #1256 recurs for it: the "
            "figures survive the restart and the stamp does not",
        )


class TestTheRestoreKeepsTheStamp(unittest.TestCase):
    """The other half of the fix -- stamping is pointless if the restore then
    discards it."""

    def test_nimbus_version_is_no_longer_in_the_dropped_set(self):
        import inspect

        from custom_components.nimbus_load import sensor as sensor_module

        src = inspect.getsource(
            sensor_module._NimbusSolverPushSensor._async_restore_last_value
        )
        # The dropped set is a literal tuple in the comprehension; the name
        # must not appear as a dropped key. It legitimately appears in the
        # docstring explaining why it is NOT dropped, so match the literal.
        body = src.split('self._attrs = {', 1)[1]
        self.assertNotIn(
            '"nimbus_version"',
            body,
            "restoring the figures without their stamp is what let the "
            "property re-inject the running version onto them",
        )

    def test_the_ha_managed_keys_are_still_dropped(self):
        """Loosening one key must not loosen the rest -- restoring a stale
        unit or friendly_name is its own documented failure."""
        import inspect

        from custom_components.nimbus_load import sensor as sensor_module

        body = inspect.getsource(
            sensor_module._NimbusSolverPushSensor._async_restore_last_value
        ).split('self._attrs = {', 1)[1]
        for key in (
            "unit_of_measurement",
            "device_class",
            "state_class",
            "friendly_name",
            "icon",
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', body)


class TestTheEntityFallbackIsUntouched(unittest.TestCase):
    """#972's purpose -- mirror detection -- must not be traded away."""

    def test_a_publisher_that_does_not_stamp_still_gets_the_running_version(self):
        import inspect

        from custom_components.nimbus_load import sensor as sensor_module

        src = inspect.getsource(
            sensor_module._NimbusSolverPushSensor.extra_state_attributes.fget
        )
        self.assertIn('if "nimbus_version" in self._attrs:', src)
        self.assertIn('"nimbus_version": self._sw_version', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
