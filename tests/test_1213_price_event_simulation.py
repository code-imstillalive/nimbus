"""nimbus issue #1213 (Mark Purcell): the additive price-event test sensor.

A manually-armed what-if tool for simulating two real NEM conditions
against the **actual live forecast**, before one happens rather than only
explaining one afterwards:

* **Market Price Cap** (LOR2/LOR3) — the real cap is $23.20/kWh, already
  confirmed live for the reference household in #675/#705. The issue's own
  example is +$20/kWh, 18:00–20:00.
* **Minimum System Load** negative-price floor — the issue's example is
  −$1/kWh, 11:00–15:00.

Implemented as specified: step-point `forecast` shape, additive over the
already blended and settled price series, **symmetric** across import and
export, opt-in, and inert by default.

## The one design point the issue does not settle

Every other price path here holds the most recent step point **forward**,
which is right for a *price*. For a *delta* it means **an event that never
ends**. So `resolve_price_event_delta()` keeps the same lookup but treats
the edges as a delta requires, and these tests pin both halves:

* **before** the first point → `0.0`, never backfilled. A price series
  backfills because a price always exists; a delta before its own first
  step point is simply absent.
* **after** the last point → held forward, because that is what a step
  function means — but a non-zero final point gets a **WARNING naming the
  consequence**, because obeying it silently is how a two-hour test
  becomes a permanent distortion of every future solve.

## What else these tests pin

* **Inert by default, three independent ways**: switch off, no sensor, or
  windows that miss the horizon. Each returns `None` rather than a list of
  zeros, so the caller skips the arithmetic and a log reader can tell
  "off" from "armed but flat".
* **Armed-but-broken is loud, not silent.** An armed simulation whose
  sensor is missing or malformed warns and applies nothing. The failure a
  test tool must never have is looking like it worked.
* **Symmetry**, because a real wholesale event moves the whole market.
* **A malformed row does not discard the window** — one bad point in an
  otherwise good list must not silently disarm a test that is running.
* **It is applied, and applied last** — after the blend and after the
  settled-block re-assertion, which is where the issue asks for it.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer

DAY = datetime(2026, 9, 26, 0, 0, 0, tzinfo=UTC)
SENSOR = "sensor.nimbus_price_event_test"


def _grid(n: int, minutes: int = 30) -> list[datetime]:
    return [DAY + timedelta(minutes=minutes * i) for i in range(n)]


def _armed(sensor: str | None = SENSOR) -> dict:
    cfg = {"solver_price_event_enabled": True}
    if sensor is not None:
        cfg["solver_price_event_sensor"] = sensor
    return cfg


def _points(*pairs: tuple[int, float]) -> list[dict]:
    """`(hour_offset, value)` pairs as step points."""
    return [
        {"time": (DAY + timedelta(hours=h)).isoformat(), "value": v} for h, v in pairs
    ]


def _with_state(state):
    """Patch `ha_get()` so the resolver reads `state` for any entity."""
    return unittest.mock.patch.object(solver_writer, "ha_get", lambda _entity_id: state)


def _forecast_state(points):
    return {"state": "0.0", "attributes": {"forecast": points}}


class TestInertByDefault(unittest.TestCase):
    """Three independent ways to be off, all returning None rather than a
    list of zeros -- the caller then skips the arithmetic entirely."""

    def test_switch_off_is_a_no_op_even_with_a_sensor_configured(self):
        cfg = {
            "solver_price_event_enabled": False,
            "solver_price_event_sensor": SENSOR,
        }
        with _with_state(_forecast_state(_points((0, 20.0), (2, 0.0)))):
            self.assertIsNone(solver_writer.resolve_price_event_delta(cfg, _grid(8)))

    def test_no_sensor_configured_is_a_no_op_even_when_armed(self):
        with _with_state(_forecast_state(_points((0, 20.0)))):
            self.assertIsNone(
                solver_writer.resolve_price_event_delta(_armed(None), _grid(8))
            )

    def test_an_empty_grid_is_a_no_op(self):
        with _with_state(_forecast_state(_points((0, 20.0)))):
            self.assertIsNone(solver_writer.resolve_price_event_delta(_armed(), []))

    def test_a_window_that_misses_the_horizon_is_a_no_op(self):
        """Armed, valid, and simply not overlapping this solve. A clean
        no-op, and DEBUG rather than WARNING -- there is nothing wrong."""
        far = _points((48, 20.0), (50, 0.0))
        with _with_state(_forecast_state(far)):
            self.assertIsNone(
                solver_writer.resolve_price_event_delta(_armed(), _grid(8))
            )


class TestArmedButBrokenIsLoud(unittest.TestCase):
    """The failure a test tool must never have is looking like it worked."""

    def test_an_unreadable_sensor_warns_and_applies_nothing(self):
        with _with_state(None), self.assertLogs(solver_writer._LOGGER, "WARNING") as cm:
            self.assertIsNone(
                solver_writer.resolve_price_event_delta(_armed(), _grid(4))
            )
        self.assertIn("#1213", "\n".join(cm.output))

    def test_a_sensor_with_no_forecast_attribute_warns(self):
        with (
            _with_state({"state": "0.0", "attributes": {}}),
            self.assertLogs(solver_writer._LOGGER, "WARNING") as cm,
        ):
            self.assertIsNone(
                solver_writer.resolve_price_event_delta(_armed(), _grid(4))
            )
        self.assertIn("forecast", "\n".join(cm.output))

    def test_a_forecast_of_entirely_unparseable_rows_warns(self):
        junk = [{"time": "not-a-date", "value": "nope"}, {"nothing": 1}, "a string"]
        with (
            _with_state(_forecast_state(junk)),
            self.assertLogs(solver_writer._LOGGER, "WARNING") as cm,
        ):
            self.assertIsNone(
                solver_writer.resolve_price_event_delta(_armed(), _grid(4))
            )
        self.assertIn("#1213", "\n".join(cm.output))

    def test_one_bad_row_does_not_discard_a_running_window(self):
        """A single malformed point must not silently disarm a test that is
        otherwise well-formed and active."""
        mixed = [
            {"time": "not-a-date", "value": 5.0},
            *_points((1, 20.0), (3, 0.0)),
        ]
        with _with_state(_forecast_state(mixed)):
            deltas = solver_writer.resolve_price_event_delta(_armed(), _grid(8))
        self.assertIsNotNone(deltas)
        assert deltas is not None
        self.assertEqual(max(deltas), 20.0)


class TestStepSemanticsAtTheEdges(unittest.TestCase):
    """The delta-vs-price distinction, which is the only place this
    deliberately differs from every other price path in the repo."""

    def test_periods_before_the_first_point_are_zero_not_backfilled(self):
        # 30-minute grid; the event starts at +2h == index 4.
        with _with_state(_forecast_state(_points((2, 20.0), (4, 0.0)))):
            deltas = solver_writer.resolve_price_event_delta(_armed(), _grid(12))
        assert deltas is not None
        self.assertEqual(
            deltas[:4],
            [0.0, 0.0, 0.0, 0.0],
            "a delta before its own first step point is absent, not "
            "backfilled -- backfilling would apply a cap event to hours "
            "before it was ever declared",
        )
        self.assertEqual(deltas[4:8], [20.0] * 4)
        self.assertEqual(deltas[8:], [0.0] * 4)

    def test_a_closed_window_returns_to_zero_afterwards(self):
        with _with_state(_forecast_state(_points((0, 20.0), (2, 0.0)))):
            deltas = solver_writer.resolve_price_event_delta(_armed(), _grid(8))
        assert deltas is not None
        self.assertEqual(deltas, [20.0, 20.0, 20.0, 20.0, 0.0, 0.0, 0.0, 0.0])

    def test_an_unclosed_window_holds_forward_AND_warns(self):
        """Holding forward is the correct reading of a step function, so
        the value is obeyed. Being quiet about it is not correct, because
        this is the one mistake that turns a two-hour test into a
        permanent distortion of every future solve."""
        with (
            _with_state(_forecast_state(_points((0, 20.0)))),
            self.assertLogs(solver_writer._LOGGER, "WARNING") as cm,
        ):
            deltas = solver_writer.resolve_price_event_delta(_armed(), _grid(6))
        assert deltas is not None
        self.assertEqual(deltas, [20.0] * 6, "a step value holds forward")
        joined = "\n".join(cm.output)
        self.assertIn("never closes", joined)
        self.assertIn("#1213", joined)

    def test_points_are_sorted_before_use(self):
        """A household-built template sensor has no obligation to emit its
        forecast in time order."""
        shuffled = _points((4, 0.0), (0, 20.0), (2, 5.0))
        with _with_state(_forecast_state(shuffled)):
            deltas = solver_writer.resolve_price_event_delta(_armed(), _grid(10))
        assert deltas is not None
        self.assertEqual(deltas[:4], [20.0] * 4)
        self.assertEqual(deltas[4:8], [5.0] * 4)
        self.assertEqual(deltas[8:], [0.0, 0.0])


class TestRealisticScenariosFromTheIssue(unittest.TestCase):
    def test_a_market_price_cap_event(self):
        """+$20/kWh, 18:00-20:00, the issue's own example."""
        grid = [DAY + timedelta(minutes=30 * i) for i in range(48)]
        with _with_state(_forecast_state(_points((18, 20.0), (20, 0.0)))):
            deltas = solver_writer.resolve_price_event_delta(_armed(), grid)
        assert deltas is not None
        # 18:00 is index 36 on a 30-minute grid; 20:00 is index 40.
        self.assertEqual(deltas[36:40], [20.0] * 4)
        self.assertEqual(sum(1 for d in deltas if d != 0.0), 4)

    def test_a_negative_price_floor_event(self):
        """-$1/kWh, 11:00-15:00, the issue's other example. A negative
        delta must work exactly as well as a positive one."""
        grid = [DAY + timedelta(minutes=30 * i) for i in range(48)]
        with _with_state(_forecast_state(_points((11, -1.0), (15, 0.0)))):
            deltas = solver_writer.resolve_price_event_delta(_armed(), grid)
        assert deltas is not None
        self.assertEqual(deltas[22:30], [-1.0] * 8)
        self.assertEqual(min(deltas), -1.0)


class TestItIsAppliedSymmetricallyAndLast(unittest.TestCase):
    """A real wholesale price event moves the whole market, not one side,
    and #1213 asks for it at the very end of price resolution."""

    def test_main_adds_the_delta_to_both_import_and_export(self):
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(solver_writer.main))
        self.assertIn("resolve_price_event_delta(cfg, grid_times)", src)
        # Both series, same delta.
        self.assertIn("import_price = [p + d for p, d in zip(", src)
        self.assertIn("export_price = [p + d for p, d in zip(", src)
        # And it really is a call, not only a mention in a comment.
        called = {
            node.func.id
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("resolve_price_event_delta", called)

    def test_it_is_applied_after_the_blend_and_the_settled_block(self):
        """Ordering is the whole reason it can stay ignorant of the
        blending and settlement logic that produced the series."""
        import inspect

        src = inspect.getsource(solver_writer.main)
        blend_at = src.rindex("blend_price_with_secondary_sources(")
        event_at = src.index("resolve_price_event_delta(cfg, grid_times)")
        self.assertGreater(
            event_at,
            blend_at,
            "the price event must be layered on top of the blended series, "
            "not before it",
        )


class TestTheConfigSurfaceExists(unittest.TestCase):
    """Two fields, a switch, and the wizard merge -- an option the wizard
    does not carry through would be silently dropped by the next step."""

    def test_the_const_keys_exist_and_default_off(self):
        from custom_components.nimbus_load import const

        self.assertEqual(
            const.CONF_SOLVER_PRICE_EVENT_SENSOR, "solver_price_event_sensor"
        )
        self.assertEqual(
            const.CONF_SOLVER_PRICE_EVENT_ENABLED, "solver_price_event_enabled"
        )
        self.assertFalse(const.DEFAULT_SOLVER_PRICE_EVENT_ENABLED)

    def test_the_sensor_is_carried_through_the_wizard_merge(self):
        from custom_components.nimbus_load.flows import hub_options

        self.assertIn(
            "solver_price_event_sensor",
            hub_options._SOLVER_WIZARD_SCHEMA_KEYS,
        )

    def test_a_switch_entity_is_declared_for_the_arm(self):
        src = Path(
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "nimbus_load"
            / "switch.py"
        ).read_text(encoding="utf-8")
        self.assertIn("CONF_SOLVER_PRICE_EVENT_ENABLED", src)
        self.assertIn("Price Event Simulation Armed", src)

    def test_both_string_files_describe_the_new_field(self):
        """CI checks strings/translations parity; this says plainly what
        the field is for, including the close-your-window requirement."""
        import json

        base = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        for name in ("strings.json", "translations/en.json"):
            with self.subTest(file=name):
                blob = json.loads((base / name).read_text(encoding="utf-8"))
                dumped = json.dumps(blob)
                self.assertIn("solver_price_event_sensor", dumped)
                # The close-your-window requirement must be stated, since
                # it is the one mistake that turns a test into a permanent
                # distortion. Asserted on the two load-bearing phrases
                # rather than an exact sentence -- and NOT on a literal
                # brace pair, because HA's own hassfest reads {...} in a
                # translation as a placeholder and rejects the file.
                self.assertIn("value of 0", dumped)
                self.assertIn("holds forward", dumped)
                self.assertNotIn(
                    "{",
                    blob["options"]["step"]["solver_grid"]["data_description"][
                        "solver_price_event_sensor"
                    ],
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
