"""nimbus issue #448: guard the config-surface counts the architecture
proposal argues from, so they cannot go stale.

#448's thesis is that Nimbus's *user-facing* config surface grew
alongside its engine without a simplification pass. That argument is
made entirely out of numbers — and when the issue was re-measured nine
days after filing, every number in it had moved:

    _SOLVER_WIZARD_SCHEMA_KEYS   22 -> 26
    sensor.py sensor classes     13 -> 30

Nothing had re-checked them, because nothing could fail. This file makes
the counts a build artifact instead of a sentence someone has to
remember to re-run.

## What it is actually for, which is not "keep the number at 46"

The useful finding from that re-measurement was **not** that fields are
added carelessly. Each addition has its own issue and its own reason, and
three fields were genuinely *retired* into auto-discovery in the same
window (#495's geocoded region, #452's AEMO forecast sensor, #768's
per-load power sensor). The problem is that **retirement runs slower than
addition, and nothing measures the balance.**

So this test is a prompt, not a veto. Changing a number here is a
completely legitimate thing to do — it just has to be a decision someone
makes on purpose, in the same commit that changes the surface, rather
than a drift nobody sees. Mark Purcell's own standing steer (#449,
2026-09-14) is the question to ask when it fires:

    "As a general principal we should be using less/ remove wizard
    configuration values and more sensible default which are surfaced as
    configuration entities on the respective devices."

## Why AST and not a grep, which is load-bearing

Counting `vol.Required(` / `vol.Optional(` calls in the source reports
`_switchboard_schema` as **one** field. It has **eight** — that schema is
built in a loop, so a single call serves all of them. That undercount
nearly went into #448's own comment as a published number.

Two consequences, both deliberate here:

- The authoritative per-form totals come from each form's own
  `_*_SCHEMA_KEYS` tuple, which exists precisely so it cannot drift from
  the form it describes.
- The per-step `Required`/`Optional` split is counted from the schema
  builder functions, because there is no per-step key tuple — and that
  split is the interesting half, since it is what shows the wizard is 5
  real decisions wrapped in 41 optional refinements.

Parsed, never imported: `flows/hub_options.py` pulls in `homeassistant.*`
and this file needs no stub to count a tuple.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_HUB_OPTIONS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "flows"
    / "hub_options.py"
)

# Per-form totals, from each form's own key tuple.
EXPECTED_KEY_COUNTS = {
    "_SWITCHBOARD_SCHEMA_KEYS": 8,
    "_FORECASTER_SCHEMA_KEYS": 12,
    "_SOLVER_WIZARD_SCHEMA_KEYS": 26,
}

# (required, optional) per schema builder. `_switchboard_schema` reads as
# (0, 1) on purpose -- see this module's docstring: its eight fields come
# from one call inside a loop. That row is the counting trap, kept visible
# rather than hidden behind a number that looks right.
EXPECTED_SPLIT = {
    "_forecaster_schema": (0, 12),
    "_solver_battery_schema": (1, 1),
    "_solver_grid_schema": (2, 6),
    "_solver_sources_schema": (2, 14),
    "_switchboard_schema": (0, 1),
}

_WHEN_THIS_FIRES = (
    "\n\nThis is not a failure -- it is the prompt #448 asked for. The "
    "config surface changed, so decide on purpose and update the number "
    "in the same commit:\n"
    "  * ADDED a field? Ask Mark's standing question first (#449): could "
    "this be auto-discovered, or a sensible default surfaced as a "
    "config entity on the device instead? Three fields have been retired "
    "that way already (#495, #452, #768).\n"
    "  * RETIRED a field? Lower the number and say so -- that is the "
    "direction this guard exists to make visible, and it is the one "
    "nobody currently counts.\n"
    "  * Restructured a form? Check no key changed which step owns it: "
    "that is the wipe class four separate dated fixes in "
    "async_step_forecaster already exist to protect against."
)


def _tree() -> ast.Module:
    return ast.parse(_HUB_OPTIONS.read_text(encoding="utf-8"))


def _key_counts(tree: ast.Module) -> dict[str, int]:
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id.endswith("_SCHEMA_KEYS")
                and isinstance(node.value, (ast.Tuple, ast.List, ast.Set))
            ):
                out[target.id] = len(node.value.elts)
    return out


def _split(tree: ast.Module) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        req = opt = 0
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr == "Required":
                    req += 1
                elif sub.func.attr == "Optional":
                    opt += 1
        if req or opt:
            out[fn.name] = (req, opt)
    return out


class TestTheConfigSurfaceIsMeasured(unittest.TestCase):
    def test_per_form_field_counts(self):
        actual = _key_counts(_tree())
        self.assertEqual(
            {k: actual.get(k) for k in EXPECTED_KEY_COUNTS},
            EXPECTED_KEY_COUNTS,
            "a form's field count changed." + _WHEN_THIS_FIRES,
        )

    def test_the_total_a_household_actually_meets(self):
        """46 across three forms, before any subentry. #448 had been
        arguing from 26 -- the Solver step alone, under 60% of it."""
        actual = _key_counts(_tree())
        total = sum(actual.get(k, 0) for k in EXPECTED_KEY_COUNTS)
        self.assertEqual(
            total,
            sum(EXPECTED_KEY_COUNTS.values()),
            f"the whole config surface is now {total} fields, not "
            f"{sum(EXPECTED_KEY_COUNTS.values())}." + _WHEN_THIS_FIRES,
        )

    def test_required_versus_optional_split_per_form(self):
        """The interesting half. A form rendering 16 fields of which 2 are
        required is a very different thing from 16 real decisions, and
        that distinction is what #449 is about surfacing."""
        actual = _split(_tree())
        self.assertEqual(
            {k: actual.get(k) for k in EXPECTED_SPLIT},
            EXPECTED_SPLIT,
            "a form's required/optional split changed." + _WHEN_THIS_FIRES,
        )

    def test_only_five_fields_are_genuinely_required(self):
        """The number worth quoting, and the one nobody had: the wizard
        looks like 46 decisions and is 5, plus 41 optional refinements.
        Two of the three forms can be submitted entirely blank."""
        actual = _split(_tree())
        required = sum(r for r, _o in actual.values())
        self.assertEqual(
            required,
            5,
            f"the number of genuinely required fields is now {required}, "
            f"not 5. If that went UP, a household's minimum setup just got "
            f"harder -- the one direction #448 is most concerned with."
            + _WHEN_THIS_FIRES,
        )


class TestTheCountingTrapStaysDocumented(unittest.TestCase):
    """The reason this file parses instead of grepping. Kept as a test so
    the discrepancy is demonstrated rather than asserted in prose."""

    def test_switchboard_has_eight_keys_but_one_optional_call(self):
        tree = _tree()
        keys = _key_counts(tree)["_SWITCHBOARD_SCHEMA_KEYS"]
        _req, opt_calls = _split(tree)["_switchboard_schema"]
        self.assertEqual(keys, 8)
        self.assertEqual(
            opt_calls,
            1,
            "the switchboard schema no longer builds its fields in a loop. "
            "That is fine -- but this test existed to demonstrate that "
            "counting vol.Optional() calls undercounts it 8:1, so if the "
            "loop is gone, delete this test rather than 'fixing' the "
            "number, and keep the docstring's warning in "
            "test_config_surface_budget.py's module header.",
        )


if __name__ == "__main__":
    unittest.main()
