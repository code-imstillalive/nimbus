"""nimbus #1236: an install that commits real P2P export but has no
settlement sensor is warned about it, instead of silently scoring every
day P2P-blind forever.

**The silence.** `real_p2p_settlement_status = "no_sensor_configured"` is
deliberately excluded from `_PROVISIONAL_SETTLEMENT_STATUSES`, so #1201's
repair sweep will never revisit such a day -- correctly, because there is
nothing to wait for. But the day is then priced with zero P2P export
credit and `real_p2p_dollars: 0.0`, which is indistinguishable from a
household that genuinely earns no P2P. No error, no flag, an
ordinary-looking number, on every day, permanently.

Measured on the reference household, the cost of being in that state is
not marginal: 24 Sep read **41.6%** scored P2P-blind against **68.85%**
with real settlement applied (#1200), and 21-23 Sep read 51.9/38.5/36.4%
against 89.8/87.7/87.2% once $14.91/$12.71/$13.46 of settled export was
credited.

The sibling status already had this reasoning applied -- the comment on
`window_is_not_one_local_calendar_day` ends *"The gate itself is right ...
but being silent about it is not."* This is that argument applied to the
case it skipped.

The properties pinned here:

1. **The warning exists**, on the no-settlement-sensor path, and cites the
   issue so it is traceable.
2. **It is GATED, not unconditional.** An install with no P2P scheme at
   all must stay silent. An unconditional warning would fire on every
   scored day of every install that does not use the feature, and a
   warning that is always present is one nobody reads -- this repo already
   maintains a log noise-filter for exactly that failure.
3. **The gate is the inconsistent combination**: no settlement sensor AND
   a real committed P2P block (`fixed_export_kw is not None`, which is
   exactly "at least one block has rate_kw > 0" per
   `fetch_p2p_fixed_export_kw()`'s own contract).
4. **It is actionable** -- it names the setting to change, not just the
   symptom.
5. **Control: the status taxonomy is unchanged.** The fix is the warning
   only. Making `no_sensor_configured` provisional would retry forever on
   an install that will never have a sensor, at a full oracle MIP each
   time -- the #773 executor-starvation shape.
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer


def _window_src() -> str:
    return inspect.getsource(solver_writer._compute_report_for_window)


_TREE: ast.AST | None = None


def _tree() -> ast.AST:
    """Parse ONCE and reuse.

    Re-parsing per call produced a fresh node object each time, so the
    guard lookup compared nodes from two different trees by identity and
    never matched -- the test failed claiming the warning was
    unconditional when it was not. Caught because it failed loudly rather
    than passing vacuously, which is the only reason it was cheap.
    """
    global _TREE
    if _TREE is None:
        _TREE = ast.parse(textwrap.dedent(_window_src()))
    return _TREE


def _warning_calls() -> list[ast.Call]:
    """Every `_LOGGER.warning(...)` call in the scorer, as AST nodes."""
    tree = _tree()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "warning"
            and isinstance(f.value, ast.Name)
            and f.value.id == "_LOGGER"
        ):
            out.append(node)
    return out


def _the_1236_warning() -> ast.Call:
    for call in _warning_calls():
        for arg in call.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and "1236" in arg.value
            ):
                return call
            # a message split across implicitly-concatenated literals
            # arrives already joined as one Constant, but be tolerant
            if isinstance(arg, ast.JoinedStr):
                joined = "".join(
                    v.value
                    for v in arg.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                if "1236" in joined:
                    return call
    raise AssertionError(
        "no _LOGGER.warning citing nimbus issue 1236 found in "
        "_compute_report_for_window"
    )


class TestTheWarningExists(unittest.TestCase):
    """Property 1."""

    def test_a_warning_cites_the_issue(self):
        call = _the_1236_warning()
        self.assertIsInstance(call, ast.Call)

    def test_it_says_the_status_is_permanent_not_provisional(self):
        """The whole point: a household must understand this will never
        fix itself, unlike the provisional statuses."""
        src = _window_src()
        i = src.find("1236")
        window = src[max(0, i - 1400) : i + 400]
        self.assertIn("permanent", window.lower())
        self.assertIn("provisional", window.lower())


class TestItIsGatedNotUnconditional(unittest.TestCase):
    """Properties 2 and 3 -- the part that matters most."""

    def _guard(self) -> ast.If:
        """The `if` statement that guards the #1236 warning."""
        target = _the_1236_warning()
        for node in ast.walk(_tree()):
            if not isinstance(node, ast.If):
                continue
            for inner in ast.walk(node):
                if inner is target:
                    return node
        raise AssertionError(
            "the #1236 warning is NOT inside an `if` -- it "
            "would fire on every scored day of every "
            "install, including those with no P2P at all"
        )

    def test_the_warning_is_inside_a_conditional(self):
        self.assertIsInstance(self._guard(), ast.If)

    def test_the_gate_requires_a_real_committed_p2p_block(self):
        """`fixed_export_kw is not None` is exactly "at least one P2P
        block has rate_kw > 0" -- so a household with no P2P scheme stays
        silent."""
        test_src = ast.unparse(self._guard().test)
        self.assertIn("fixed_export_kw", test_src, test_src)
        self.assertIn("None", test_src, test_src)

    def test_the_gate_requires_the_sensor_to_be_absent(self):
        test_src = ast.unparse(self._guard().test)
        self.assertIn("settlement_sensor", test_src, test_src)

    def test_the_gate_is_a_conjunction_of_both(self):
        """Either condition alone is wrong: sensor-absent alone nags every
        non-P2P install, block-configured alone fires even when the sensor
        is correctly set."""
        guard = self._guard().test
        self.assertIsInstance(
            guard,
            ast.BoolOp,
            "expected both conditions ANDed; got " + ast.unparse(guard),
        )
        self.assertIsInstance(guard.op, ast.And, ast.unparse(guard))


class TestItIsActionable(unittest.TestCase):
    """Property 4 -- a warning that names no remedy wastes the reader's
    time."""

    def test_it_names_the_setting_to_change(self):
        call = _the_1236_warning()
        msg = " ".join(
            a.value
            for a in call.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        )
        self.assertIn("settlement", msg.lower())
        self.assertTrue(
            "solver settings" in msg.lower() or "configure" in msg.lower(),
            "the warning should point at where to fix it: " + msg,
        )

    def test_it_names_the_day_it_is_talking_about(self):
        """A warning about "a day" with no date cannot be correlated with
        anything."""
        call = _the_1236_warning()
        self.assertTrue(
            call.args[1:],
            "expected the scored date passed as a lazy-format argument",
        )


class TestTheTaxonomyIsUnchanged(unittest.TestCase):
    """Property 5 -- control. The fix is the warning, not a behaviour
    change."""

    def test_no_sensor_configured_is_still_not_provisional(self):
        self.assertNotIn(
            "no_sensor_configured",
            solver_writer._PROVISIONAL_SETTLEMENT_STATUSES,
            "making this provisional would retry forever on an install "
            "that will never have a sensor, at a full oracle MIP each "
            "time -- the #773 starvation shape",
        )

    def test_the_provisional_set_is_exactly_the_two_transient_statuses(self):
        self.assertEqual(
            set(solver_writer._PROVISIONAL_SETTLEMENT_STATUSES),
            {"no_settlement_entry_for_this_date", "settlement_sensor_unreadable"},
        )


if __name__ == "__main__":
    unittest.main()
