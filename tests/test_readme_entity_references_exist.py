"""Regression test for nimbus issue #364 (Mark Purcell, codebase review):
`README.md` never mentioned `sensor.nimbus_health_report`, `switch.nimbus_
solver_dispatch_dry_run`/`sensor.nimbus_solver_dispatch_dry_run`, `sensor.
nimbus_solver_price_response_latency`, `sensor.nimbus_mirror_{temperature,
humidity}_forecast`, or the `compute_quality_report` service, despite all
of them being real, live entities/services -- a reviewer grepping the docs
for any of these would conclude they don't exist.

Fixed by adding a real "Diagnostics, dry-run, and other hub sensors" +
"Services" section to README.md. This test guards the OTHER direction of
staleness (the exact failure mode that made `solver/README.md`'s own
"not wired into anything" claim go unnoticed for months): every entity_id/
service string README.md now names is checked against the real source, so
a future rename that isn't also reflected in the README fails loudly here
instead of silently drifting.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"
_NIMBUS_LOAD = _REPO_ROOT / "custom_components" / "nimbus_load"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestReadmeEntityAndServiceReferencesAreReal(unittest.TestCase):
    def setUp(self):
        self.readme_text = _read(_README)
        self.sensor_py = _read(_NIMBUS_LOAD / "sensor.py")
        self.switch_py = _read(_NIMBUS_LOAD / "switch.py")
        self.services_py = _read(_NIMBUS_LOAD / "services.py")

    def _assert_readme_mentions(self, needle: str) -> None:
        self.assertIn(
            needle,
            self.readme_text,
            f"README.md no longer mentions {needle!r} -- if this entity/"
            "service was renamed or removed, update the README's own "
            "'Diagnostics, dry-run, and other hub sensors'/'Services' "
            "section rather than just deleting this assertion",
        )

    def test_health_report_sensor_documented_and_real(self):
        self._assert_readme_mentions("sensor.nimbus_health_report")
        self.assertIn('"sensor.nimbus_health_report"', self.sensor_py)

    def test_dispatch_dry_run_documented_and_real(self):
        self._assert_readme_mentions("sensor.nimbus_solver_dispatch_dry_run")
        self._assert_readme_mentions("switch.nimbus_solver_dispatch_dry_run")
        self.assertIn("nimbus_solver_dispatch_dry_run", self.sensor_py)
        self.assertIn("CONF_SOLVER_DISPATCH_DRY_RUN", self.switch_py)

    def test_price_response_latency_documented_and_real(self):
        self._assert_readme_mentions("sensor.nimbus_solver_price_response_latency")
        self.assertIn('"sensor.nimbus_solver_price_response_latency"', self.sensor_py)

    def test_mirror_forecast_sensors_documented_and_real(self):
        self._assert_readme_mentions("sensor.nimbus_mirror_temperature_forecast")
        self._assert_readme_mentions("sensor.nimbus_mirror_humidity_forecast")
        self.assertIn("nimbus_mirror_temperature_forecast", self.sensor_py)
        self.assertIn("nimbus_mirror_humidity_forecast", self.sensor_py)

    def test_compute_quality_report_service_documented_and_real(self):
        self._assert_readme_mentions("nimbus_load.compute_quality_report")
        self.assertIn(
            'SERVICE_COMPUTE_QUALITY_REPORT = "compute_quality_report"',
            self.services_py,
        )

    def test_retrain_and_solve_now_services_documented_and_real(self):
        self._assert_readme_mentions("nimbus_load.retrain")
        self._assert_readme_mentions("nimbus_load.solve_now")
        self.assertIn('SERVICE_RETRAIN = "retrain"', self.services_py)
        self.assertIn('SERVICE_SOLVE_NOW = "solve_now"', self.services_py)

    def test_every_domain_service_constant_is_mentioned_somewhere_in_readme(self):
        # Broader net: catch a FUTURE new service that never gets added to
        # the README at all, not just the three named above.
        service_constants = re.findall(
            r'^SERVICE_\w+\s*=\s*"([a-z_]+)"', self.services_py, re.MULTILINE
        )
        self.assertTrue(
            service_constants, "no SERVICE_* constants found -- test setup issue"
        )
        for service_name in service_constants:
            with self.subTest(service=service_name):
                self.assertIn(
                    f"nimbus_load.{service_name}",
                    self.readme_text,
                    f"service '{service_name}' exists in services.py but "
                    "README.md never mentions it",
                )
