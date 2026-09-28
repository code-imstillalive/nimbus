"""`build_oracle_controllable_loads()` turns reconstructions into oracle inputs
(nimbus issue #1357, step 2).

Step 1 (#1388) gave `compute_quality_report()` the ability to re-time a day's
Controllable Loads and left nothing passing it. This is the builder that does,
plus the switch that gates it.

## What these pin

1. **Off is a true no-op.** The switch is off by default, and with it off the
   report is byte-for-byte what it was — which is what
   `test_768_delivery_is_published_without_moving_the_score.py` already asserts
   and must keep asserting.

2. **`target_kwh` is the REAL delivered energy, never the configured target.**
   #768's decision. A day where the tank got half its target must be scored
   against having delivered half; charging the oracle the configured target
   would grade it against work the house never did and make every such day look
   like a dispatch failure. This is the single most reversible-looking line in
   the builder and the one most likely to be "helpfully" changed.

3. **Every skip is reported with a reason.** `kind=thermal` (excluded per #768),
   an unscorable reconstruction, a window that does not resolve, no delivered
   energy. A partially-scored day must not read as a wholly-scored one.

4. **The subtraction array sums only the loads that actually entered.** A
   skipped load's energy must stay in the oracle's base load, because the oracle
   is not being given the freedom to move it. Getting this wrong removes energy
   and never gives it back — which `OracleControllableLoads.__post_init__`
   cannot catch, because the array is present and the loads are non-empty.

5. **The same-day-in-progress correction is not applied.** A scored window has
   entirely elapsed; `_earliest_period_for_same_day_window()` exists because a
   FORWARD plan's `now` can sit inside the window. Applying it retrospectively
   would move the window for a reason that does not exist.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path
import numpy as np
import solver_writer
from solver_inputs import controllable_load_history as clh

N = 24
PERIOD_HOURS = [1.0] * N
START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
GRID_TIMES = [START + timedelta(hours=i) for i in range(N)]


class _Sub:
    def __init__(self, subentry_id, data):
        self.subentry_id = subentry_id
        self.subentry_type = "controllable_load"
        self.data = data


class _Entry:
    def __init__(self, subs):
        self.subentries = {s.subentry_id: s for s in subs}


class _ConfigEntries:
    def __init__(self, entry):
        self._entry = entry

    def async_entries(self, _domain):
        return [self._entry] if self._entry is not None else []


class _Hass:
    def __init__(self, subs):
        self.config_entries = _ConfigEntries(_Entry(subs) if subs is not None else None)


def _delivery(**kw):
    base = {
        "subentry_id": "s1",
        "name": "Dishwasher",
        "kind": "deferrable",
        "power_sensor": "sensor.dw_power",
        "scorable": True,
        "reason": clh.REASON_OK,
        "delivered_kwh": 4.0,
        "delivered_kwh_by_period": tuple(
            2.0 if i in (18, 19) else 0.0 for i in range(N)
        ),
        "n_periods": N,
    }
    base.update(kw)
    return clh.ControllableLoadDelivery(**base)


DEFERRABLE_DATA = {
    "controllable_load_name": "Dishwasher",
    "controllable_load_kind": "deferrable",
    "deferrable_max_power_kw": 2.0,
    "deferrable_target_kwh": 99.0,  # deliberately NOT what the oracle should use
    "deferrable_earliest_hour": 0.0,
    "deferrable_deadline_hour": 23.0,
    "deferrable_shortfall_price": 4.0,
}
SHEDDABLE_DATA = {
    "controllable_load_name": "Pool",
    "controllable_load_kind": "sheddable",
    "sheddable_shed_cost": 0.45,
    "sheddable_min_fraction": 0.25,
}


def _build(deliveries, subs):
    with patch.object(solver_writer, "_NATIVE_HASS", _Hass(subs)):
        return clh.build_oracle_controllable_loads(
            deliveries=deliveries,
            grid_times=GRID_TIMES,
            period_hours=PERIOD_HOURS,
            n_periods=N,
            window_start=START,
        )


class TestNothingToScore(unittest.TestCase):
    def test_no_deliveries_returns_none(self):
        """None, not an empty bundle: it is the value that keeps
        compute_quality_report() on its pre-#1357 path exactly."""
        self.assertIsNone(_build([], [_Sub("s1", DEFERRABLE_DATA)]))

    def test_no_config_entry_returns_none(self):
        """A standalone/cron deployment has no subentries at all."""
        self.assertIsNone(_build([_delivery()], None))


class TestTheRealDeliveredEnergyIsTheTarget(unittest.TestCase):
    def test_target_kwh_is_delivered_not_configured(self):
        b = _build([_delivery(delivered_kwh=4.0)], [_Sub("s1", DEFERRABLE_DATA)])
        self.assertEqual(len(b.adequacy), 1)
        self.assertAlmostEqual(
            b.adequacy[0].target_kwh,
            4.0,
            msg="target_kwh must be the REAL delivered energy (4.0), not the "
            "configured deferrable_target_kwh (99.0). Scoring the oracle against "
            "a target the house never met grades it on work that never happened "
            "-- nimbus #768's own decision.",
        )

    def test_the_configured_target_is_not_read_at_all(self):
        """Same assertion from the other side: change only the configured target
        and nothing about the built config moves."""
        a = _build([_delivery()], [_Sub("s1", DEFERRABLE_DATA)])
        other = dict(DEFERRABLE_DATA, deferrable_target_kwh=1.0)
        b = _build([_delivery()], [_Sub("s1", other)])
        self.assertEqual(a.adequacy[0].target_kwh, b.adequacy[0].target_kwh)

    def test_the_real_config_values_are_carried_through(self):
        b = _build([_delivery()], [_Sub("s1", DEFERRABLE_DATA)])
        cfg = b.adequacy[0]
        self.assertEqual(cfg.max_power_kw, 2.0)
        self.assertEqual(cfg.shortfall_price, 4.0)
        self.assertEqual(cfg.subentry_id, "s1")


class TestSheddable(unittest.TestCase):
    def test_the_profile_is_the_reconstructed_kw(self):
        """`forecast_kw` is what the load really drew, period by period -- the
        LP may shed any of it, which is the freedom the household has."""
        d = _delivery(kind="sheddable", name="Pool")
        b = _build([d], [_Sub("s1", SHEDDABLE_DATA)])
        self.assertEqual(len(b.sheddable), 1)
        profile = b.sheddable[0].forecast_kw
        self.assertAlmostEqual(float(profile[18]), 2.0)
        self.assertAlmostEqual(float(profile[19]), 2.0)
        self.assertAlmostEqual(float(profile[0]), 0.0)

    def test_shed_cost_and_min_fraction_come_from_config(self):
        b = _build(
            [_delivery(kind="sheddable", name="Pool")], [_Sub("s1", SHEDDABLE_DATA)]
        )
        self.assertEqual(b.sheddable[0].shed_cost, 0.45)
        self.assertEqual(b.sheddable[0].min_fraction, 0.25)


class TestSkipsAreHonest(unittest.TestCase):
    def test_thermal_is_skipped_with_a_reason(self):
        b = _build(
            [_delivery(kind="thermal", name="Tank")],
            [_Sub("s1", {"controllable_load_kind": "thermal"})],
        )
        self.assertEqual(b.adequacy, ())
        self.assertEqual(b.sheddable, ())
        self.assertEqual(b.skipped, (("Tank", clh.ORACLE_SKIP_THERMAL),))

    def test_an_unscorable_reconstruction_carries_its_own_reason(self):
        """The reconstruction already says WHY it could not be scored -- no
        power sensor, or a day before that sensor existed. Reusing its reason
        rather than inventing one keeps a single vocabulary."""
        b = _build(
            [_delivery(scorable=False, reason="no power sensor configured")],
            [_Sub("s1", DEFERRABLE_DATA)],
        )
        self.assertEqual(b.skipped, (("Dishwasher", "no power sensor configured"),))

    def test_zero_delivered_energy_is_skipped(self):
        b = _build(
            [_delivery(delivered_kwh=0.0, delivered_kwh_by_period=(0.0,) * N)],
            [_Sub("s1", DEFERRABLE_DATA)],
        )
        self.assertEqual(b.skipped, (("Dishwasher", clh.ORACLE_SKIP_NO_ENERGY),))

    def test_an_unrecognised_kind_is_skipped(self):
        b = _build([_delivery(kind="something_new")], [_Sub("s1", DEFERRABLE_DATA)])
        self.assertEqual(b.skipped, (("Dishwasher", clh.ORACLE_SKIP_UNKNOWN_KIND),))

    def test_a_skip_only_bundle_has_no_subtraction_array(self):
        """`__post_init__` rejects an array with no loads, because that would
        remove energy the LP never gets back. A skip-only bundle must therefore
        carry skips alone -- and must still be returned, so the report can say
        which loads it did not score."""
        b = _build([_delivery(kind="thermal", name="Tank")], [_Sub("s1", {})])
        self.assertIsNone(b.delivered_kwh_by_period)
        self.assertEqual(len(b.skipped), 1)


class TestTheSubtractionArray(unittest.TestCase):
    def test_it_sums_only_the_loads_that_entered(self):
        """A SKIPPED load's energy must stay in the oracle's base load -- the
        oracle is not being given the freedom to move it. `__post_init__` cannot
        catch this: the array is present and the loads are non-empty either way.
        """
        scored = _delivery(subentry_id="s1", name="Dishwasher")
        skipped = _delivery(
            subentry_id="s2",
            name="Tank",
            kind="thermal",
            delivered_kwh_by_period=tuple(5.0 if i == 3 else 0.0 for i in range(N)),
        )
        b = _build(
            [scored, skipped],
            [_Sub("s1", DEFERRABLE_DATA), _Sub("s2", {})],
        )
        arr = np.asarray(b.delivered_kwh_by_period, dtype=float)
        self.assertAlmostEqual(float(arr.sum()), 4.0, places=6)
        self.assertAlmostEqual(
            float(arr[3]),
            0.0,
            msg="the skipped thermal load's 5 kWh at period 3 was subtracted "
            "from the oracle's base load without being given back as "
            "re-timeable -- that makes the oracle unreachably cheap.",
        )

    def test_two_scored_loads_both_contribute(self):
        a = _delivery(subentry_id="s1", name="Dishwasher")
        p = _delivery(
            subentry_id="s2",
            name="Pool",
            kind="sheddable",
            delivered_kwh=3.0,
            delivered_kwh_by_period=tuple(
                1.5 if i in (12, 13) else 0.0 for i in range(N)
            ),
        )
        b = _build([a, p], [_Sub("s1", DEFERRABLE_DATA), _Sub("s2", SHEDDABLE_DATA)])
        arr = np.asarray(b.delivered_kwh_by_period, dtype=float)
        self.assertAlmostEqual(float(arr.sum()), 7.0, places=6)
        self.assertEqual(
            sorted(b.adequacy[0].name for _ in (0,)) + [b.sheddable[0].name],
            ["Dishwasher", "Pool"],
        )


class TestTheWindowIsResolvedRetrospectively(unittest.TestCase):
    def test_the_same_day_in_progress_correction_is_not_applied(self):
        """`build_controllable_loads()` calls
        `_earliest_period_for_same_day_window()` because a FORWARD plan's `now`
        can sit inside the window. A scored window has entirely elapsed, so
        applying that correction would move the window for a reason that does
        not exist retrospectively. Asserted over the AST's CALLS rather than the
        source text, because the docstring names the helper in order to explain
        why it is absent -- a text search finds that and reports the opposite of
        the truth."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(clh.build_oracle_controllable_loads))
        )
        called = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertNotIn(
            "_earliest_period_for_same_day_window",
            called,
            "the builder now CALLS the forward plan's in-progress-window "
            "correction on a window that has entirely elapsed.",
        )
        self.assertIn(
            "_resolve_hour_to_period_index",
            called,
            "the builder no longer resolves the configured hours to periods.",
        )

    def test_an_inverted_window_is_skipped_rather_than_guessed(self):
        data = dict(
            DEFERRABLE_DATA,
            deferrable_earliest_hour=22.0,
            deferrable_deadline_hour=2.0,
        )
        b = _build([_delivery()], [_Sub("s1", data)])
        self.assertEqual(b.adequacy, ())
        self.assertEqual(b.skipped, (("Dishwasher", clh.ORACLE_SKIP_NO_WINDOW),))


class TestTheSwitchIsOffByDefault(unittest.TestCase):
    def test_the_default_is_false(self):
        """Off is the design, not caution: this moves published EPR, and #768's
        thread records that the direction is not determinable from the code, so
        a household has to be able to run the same days both ways."""
        try:
            from const import DEFAULT_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED
        except ImportError:  # pragma: no cover
            from custom_components.nimbus_load.const import (  # type: ignore[no-redef]
                DEFAULT_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED,
            )
        self.assertIs(DEFAULT_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED, False)

    def test_the_key_is_exposed_for_live_resolution(self):
        """Without the key in sensor.py's own switch tuple the switch is a
        silent no-op -- the exact bug #496 hit live on devhub and #1259
        documents in the same tuple."""
        import pathlib

        src = pathlib.Path(
            _solver_path._SOLVER_PARENT  # type: ignore[attr-defined]
        ).joinpath("sensor.py")
        text = src.read_text(encoding="utf-8")
        self.assertGreaterEqual(
            text.count("CONF_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED"),
            3,
            "expected the key in sensor.py's const import, its published-config "
            "list and _SOLVER_SWITCH_ENTITY_KEYS -- missing any one of those "
            "makes the switch unreadable from cfg.",
        )


if __name__ == "__main__":
    unittest.main()
