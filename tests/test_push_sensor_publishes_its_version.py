"""nimbus issue #972: every push sensor publishes the version of the
install that produced it.

Found on a real install, the hard way, three times in one session before
being noticed. A `remote_homeassistant` mirror of ANOTHER Nimbus had
claimed the canonical entity_ids -- `sensor.nimbus_solver_battery_forecast`
and `sensor.nimbus_household_load_total_forecast` -- so reading them to
validate a freshly deployed release was reading the OTHER install's
output, which happened to be three versions behind. The local install's
own entities existed under `_3`-suffixed names and were `unavailable`.

Nothing about that was visible from the reading. `solve_diagnostics` had
the right shape, the state was fresh, `generated_at` moved every cycle --
all of it true of the other install. What eventually gave it away was an
attribute that the local code emits unconditionally being absent, which
is a coincidence, not a method: it only worked because that particular
release had added a field.

The version was already known to the entity (`sw_version`, passed in and
used for `DeviceInfo`), but `DeviceInfo` is not readable from a state
read, a template, or the REST API -- the three ways anyone actually
checks a deployment. So it could not answer the one question being asked.

`nimbus_version` makes it a one-line check: compare it against the
version you just deployed. On a mirrored sensor it reports the ORIGIN
install's version and the disagreement is immediate.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor as sensor_module


class TestPushSensorPublishesItsVersion(unittest.TestCase):
    """Exercised against the real class, not a copy of its logic -- the
    #954 lesson.

    Uses the established `_ha_stubs` harness rather than a local stub. A
    first draft guarded the import with `skipTest`, which meant all five
    assertions silently skipped and proved nothing -- the same
    guard-that-cannot-fail shape this session has already found three
    times elsewhere.
    """

    def _build(self, sw_version, attrs):
        cls = sensor_module._NimbusSolverPushSensor
        obj = object.__new__(cls)
        obj._attrs = attrs
        obj._sw_version = sw_version
        return obj

    def test_the_version_is_published_alongside_the_solver_attributes(self):
        obj = self._build("0.94.336", {"forecast": [1, 2, 3], "generated_at": "now"})
        attrs = obj.extra_state_attributes
        self.assertEqual(attrs["nimbus_version"], "0.94.336")
        self.assertEqual(attrs["forecast"], [1, 2, 3])
        self.assertEqual(attrs["generated_at"], "now")

    def test_it_is_present_before_the_first_solve(self):
        """The empty-attrs case matters most: an install whose solver has
        not published yet is exactly when someone is checking whether the
        deploy took.
        """
        self.assertEqual(
            self._build("0.94.336", {}).extra_state_attributes["nimbus_version"],
            "0.94.336",
        )

    def test_an_unknown_version_publishes_as_none_not_as_absent(self):
        """`sw_version` is `str | None` -- the integration's own version
        lookup can return None. `None` still answers the question ("this
        install does not know its version"); a missing key looks like an
        old build and sends the reader back to guessing.
        """
        attrs = self._build(None, {"generated_at": "now"}).extra_state_attributes
        self.assertIn("nimbus_version", attrs)
        self.assertIsNone(attrs["nimbus_version"])

    def test_a_version_already_in_the_attributes_is_not_overwritten(self):
        """A more specific claim from a publish path wins over this
        generic fallback. Silently replacing it would hide exactly the
        disagreement this field exists to surface.
        """
        obj = self._build("0.94.336", {"nimbus_version": "0.94.330"})
        self.assertEqual(
            obj.extra_state_attributes["nimbus_version"],
            "0.94.330",
        )

    def test_the_solver_attributes_are_not_mutated(self):
        """The returned dict is a copy. `_attrs` is handed straight from
        `update_from_solver` and is also read by the flattened-sensor
        fan-out; mutating it here would leak a display concern into
        whatever else reads that same object.
        """
        attrs_in = {"generated_at": "now"}
        obj = self._build("0.94.336", attrs_in)
        _ = obj.extra_state_attributes
        self.assertNotIn("nimbus_version", attrs_in)


if __name__ == "__main__":
    unittest.main()
