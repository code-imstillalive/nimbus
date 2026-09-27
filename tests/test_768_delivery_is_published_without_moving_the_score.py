"""nimbus issue #768: the delivery reconstruction is published, and it does
not move a single scored number.

Two claims, and the second is the one that makes this change safe to land
on an install with real money on it.

**It is published.** A reconstruction nobody can read is not checkable
against a real install, and #768's own history is a series of confident
numbers that turned out wrong for reasons only a published intermediate
would have shown (#949's blended SoC, #1012's efficiency, #1228's
mean-vs-point artifact). `sensor.nimbus_solver_quality_report` carries
`controllable_load_delivery`: one entry per CONFIGURED Controllable Load,
scorable or not.

**It changes nothing that is scored.** Mark Purcell's own sequencing,
2026-09-27: *"don't wire `adequacy_loads=`/`sheddable_loads=` into
`compute_quality_report()`'s oracle call until that reconstruction
exists... Build that first, land it as its own change, then wire the LP
plumbing ... as a second, smaller step."* So the same window, scored with
and without a Controllable Load configured, must produce identical `epr`,
`j_ach`, `j_star` and `regret_dollars`. That is asserted here rather than
argued, because "a capability present and unwired" and "a capability
present and quietly wired" are indistinguishable from a diff that adds
both a reconstruction and a report field.

`test_768_controllable_load_delivery_reconstruction.py` covers the
reconstruction's own arithmetic; this file covers only its arrival and its
inertness.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
DAY_START = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
DAY_END = DAY_START + timedelta(days=1)

SCORED_FIELDS = (
    "epr",
    "epr_pct",
    "j_ref",
    "j_ach",
    "j_star",
    "regret_dollars",
    "scored_participants",
)

_HWS = {
    "controllable_load_name": "Hot water",
    "controllable_load_kind": "deferrable",
    "controllable_load_device_entity": "water_heater.wwk302",
    "controllable_load_power_sensor": "sensor.wwk302_power",
    "deferrable_max_power_kw": 0.65,
    "deferrable_target_kwh": 4.0,
}


def _cfg():
    # Same shape as tests/test_1149_energy_decomposition.py's own fixture:
    # flat load, no solar, a cheap/expensive price split, so the oracle has
    # something real to optimise and the report is a genuine one.
    return {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": "sensor.combined_soc",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
    }


def _flat(value, start, end, step_minutes=15):
    out, t = [], start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=step_minutes)
    return out


def _prices(start, end, cheap, expensive, expensive_hour=17):
    out, t = [], start
    while t < end:
        out.append((t, expensive if t.hour >= expensive_hour else cheap))
        t += timedelta(minutes=15)
    return out


def _fetch(entity_id, start, end):
    if entity_id == "sensor.real_solar":
        return _flat(0.0, start, end)
    if entity_id == "sensor.real_load":
        return _flat(2.0, start, end)
    if entity_id == "sensor.real_battery":
        return _flat(1.0, start, end)
    if entity_id == "sensor.import_price":
        return _prices(start, end, 0.05, 0.35)
    if entity_id == "sensor.export_price":
        return _prices(start, end, 0.02, 0.10)
    if entity_id == "sensor.combined_soc":
        return _flat(50.0, start, end)
    return []


def _hws_power(_entity_id, start, end):
    """0.65 kW through hours 2, 3 and 4 and 0.0 the rest of the day -- 1.95
    kWh, sampled every 5 minutes across the whole window.

    The explicit zeros matter: a real power sensor reports 0 when the device
    is off, and without them the last 0.65 kW reading would legitimately be
    held forward for up to `MAX_SAMPLE_GAP_HOURS` and credit another 0.65
    kWh. That is the guard behaving correctly, not a defect, but it is not
    what this file is measuring.
    """
    out, t = [], start
    while t < end:
        on = start + timedelta(hours=2) <= t < start + timedelta(hours=5)
        out.append((t, 0.65 if on else 0.0))
        t += timedelta(minutes=5)
    return out


def _native_hass(subentries):
    entry = SimpleNamespace(
        entry_id="hub", subentries={s.subentry_id: s for s in subentries}
    )
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
        states=SimpleNamespace(get=lambda _eid: None),
    )


def _subentry(subentry_id, data, subentry_type="controllable_load"):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _report():
    with (
        patch.object(solver_writer, "fetch_entity_history_range", side_effect=_fetch),
        patch.object(
            solver_writer, "fetch_entity_power_history_kw", side_effect=_hws_power
        ),
    ):
        return solver_writer._compute_report_for_window(
            _cfg(), DAY_START, DAY_END, allow_partial=True
        )


class _NativeModeCase(unittest.TestCase):
    def setUp(self):
        self._orig = solver_writer._NATIVE_HASS

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig


class TestItArrivesOnTheReport(_NativeModeCase):
    def test_a_configured_load_is_reported_with_its_real_delivery(self):
        solver_writer._NATIVE_HASS = _native_hass([_subentry("s_hws", _HWS)])
        report = _report()
        assert report is not None
        (entry,) = report["controllable_load_delivery"]
        self.assertEqual(entry["subentry_id"], "s_hws")
        self.assertEqual(entry["name"], "Hot water")
        self.assertEqual(entry["kind"], "deferrable")
        self.assertEqual(entry["power_sensor"], "sensor.wwk302_power")
        self.assertTrue(entry["scorable"])
        self.assertAlmostEqual(entry["delivered_kwh"], 1.95, places=4)
        # Not the configured 4.0 kWh target: Mark's own decided convention
        # is the real delivered total (2026-09-27).
        self.assertNotAlmostEqual(entry["delivered_kwh"], 4.0, places=2)

    def test_an_install_with_no_controllable_loads_publishes_an_empty_list(self):
        solver_writer._NATIVE_HASS = _native_hass([])
        report = _report()
        assert report is not None
        # Present and empty, not absent: the reference household and devhub
        # both have zero controllable loads, and a consumer reading this
        # attribute must not have to distinguish "old report" from "no
        # loads".
        self.assertEqual(report["controllable_load_delivery"], [])

    def test_standalone_mode_publishes_an_empty_list_too(self):
        solver_writer._NATIVE_HASS = None
        report = _report()
        assert report is not None
        self.assertEqual(report["controllable_load_delivery"], [])


class TestItMovesNothingThatIsScored(_NativeModeCase):
    """The safety claim. Same window, same inputs, the only difference being
    whether a Controllable Load exists -- every scored figure identical."""

    def test_every_scored_figure_is_unchanged_by_the_reconstruction(self):
        solver_writer._NATIVE_HASS = _native_hass([])
        without = _report()
        solver_writer._NATIVE_HASS = _native_hass([_subentry("s_hws", _HWS)])
        with_load = _report()
        assert without is not None and with_load is not None

        # The premise: the reconstruction really did run and really did find
        # energy. Without this, the equality below would hold vacuously.
        self.assertEqual(without["controllable_load_delivery"], [])
        self.assertAlmostEqual(
            with_load["controllable_load_delivery"][0]["delivered_kwh"],
            1.95,
            places=4,
        )

        for name in SCORED_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(without[name], with_load[name])


if __name__ == "__main__":
    unittest.main()
