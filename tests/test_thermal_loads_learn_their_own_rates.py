"""nimbus issue #873 (Mark Purcell, decision 2026-09-16: "Build it") --
a `kind=thermal` controllable load never learned its own thermal rates.

The `last_idle_temperature` sampler and the heating-rate/idle-decay
fitter both sat INSIDE `apply_commanded_state_guard()`'s
`load_kind == "adequacy"` branch. A `kind=thermal` load -- the very kind
named for this -- reached its own branch instead, so neither ever ran
for it: `thermal_rates_source` stayed `""` (the never-learned default)
and the load was scheduled forever off the generic fallback constants.

On the reference household that meant the one real hot-water system
planned against 8.0 degC/kWh and 0.5 degC/h rather than its own measured
tank, and it was invisible from outside until #940 (v0.94.335) began
publishing `thermal_heating_rate_origin`, which reads `fallback` beside
a null learned rate.

Mark's steer was to hoist BOTH gated statements out of the adequacy
branch rather than write a parallel `kind=thermal` copy, for three
reasons that all hold: the async context already exists at the call
site, nothing in the block is adequacy-shaped, and a second copy would
join the exact drift class #357 already pays for.

**Two attempts failed before this one, and the second failure is worth
recording because nothing catches it.** Hoisting the block to just
above the `elif load_kind == "adequacy"` line looks right, compiles,
passes `ruff` and `mypy` -- and is wrong. The block itself begins with
`if (done_entity and power_sensor and ...)`, so placing it between the
chain's `if` and its `elif` silently **re-parents that `elif` onto the
hoisted gate**. The adequacy branch then only runs when the hoisted
condition is FALSE. Python accepts it; only behaviour disagrees. The
block has to go above the chain HEAD, not into the middle of it.

The first attempt's failure was simpler and also misleading: it read as
a production regression (13 dispatch tests reporting
`len(services.calls) == 0`) and was backed out, when the real cause was
`tests/_ha_stubs.py` not exporting
`entity_registry.async_entries_for_device` -- a harness gap that only
surfaced once #768's registry discovery started running for every load
kind. Failure presenting as *absence* is the #1019 shape exactly.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
).read_text(encoding="utf-8")

# Anchors carry their FULL statement text deliberately: the bare
# phrase `if load_kind == "sheddable"` also appears inside an earlier
# conditional EXPRESSION in the same function, and matching that one
# instead silently moves every position comparison below (which is
# exactly what it did on this file's first run).
_CHAIN_HEAD = 'if load_kind == "sheddable" and period_hours_arr is not None:'
_THERMAL_BRANCH = 'elif load_kind == "thermal" and period_hours_arr is not None:'
_PROJECTION_ANCHOR = "max_power_kw = data.get(CONF_DEFERRABLE_MAX_POWER_KW)"


def _guard_body() -> str:
    start = _SRC.index("def apply_commanded_state_guard(")
    return _SRC[start:]


class TestTheLearningBlockIsAboveTheBranchChain(unittest.TestCase):
    """Structural, because the defect is structural: which branch the
    code sits in is the entire bug."""

    def setUp(self):
        self.body = _guard_body()

    def _pos(self, needle: str) -> int:
        idx = self.body.find(needle)
        self.assertNotEqual(idx, -1, f"anchor moved or vanished: {needle!r}")
        return idx

    def test_the_sampler_runs_before_any_load_kind_branch(self):
        sampler = self._pos("last_idle_temperature=live_temperature")
        chain_head = self._pos(_CHAIN_HEAD)
        self.assertLess(
            sampler,
            chain_head,
            "the last_idle_temperature sampler is inside a load_kind "
            "branch again -- a kind=thermal load will stop learning "
            "(nimbus #873)",
        )

    def test_the_rate_fitter_runs_before_any_load_kind_branch(self):
        fitter = self._pos("thermal_rates_source=(")
        chain_head = self._pos(_CHAIN_HEAD)
        self.assertLess(
            fitter,
            chain_head,
            "the heating-rate/idle-decay fitter is inside a load_kind "
            "branch again (nimbus #873)",
        )

    def test_the_branch_chain_is_still_one_chain(self):
        """The trap that broke attempt 2. If the hoisted block lands
        between the chain's `if` and its `elif`, the block's own leading
        `if` adopts that `elif` -- valid Python, wrong behaviour, and
        invisible to ruff and mypy."""
        chain = self.body[self._pos(_CHAIN_HEAD) :]
        head_indent = "                if "
        # Everything from the chain head to the thermal branch must be
        # the chain itself -- no statement at the chain's own indent may
        # appear between its members.
        upto_thermal = chain[: chain.index(_THERMAL_BRANCH)]
        stray = [
            ln
            for ln in upto_thermal.split("\n")[1:]
            if ln.startswith(head_indent) and not ln.strip().startswith("#")
        ]
        self.assertEqual(
            stray,
            [],
            "a statement at the branch-chain's own indent now sits "
            "between its members, which re-parents the following `elif` "
            f"onto it: {stray}",
        )

    def test_the_adequacy_projection_stayed_behind(self):
        """This is a RELOCATION of the generic half, not of everything.
        The temperature projection reads `deferrable_max_power_kw` and is
        genuinely adequacy-specific -- if it moved too, that is a real
        behaviour change rather than the fix asked for."""
        projection = self._pos(_PROJECTION_ANCHOR)
        chain_head = self._pos(_CHAIN_HEAD)
        self.assertGreater(
            projection,
            chain_head,
            "the adequacy-specific projection was hoisted too -- #873 "
            "asked for the sampler and fitter only",
        )

    def test_the_preconditions_were_not_loosened(self):
        """Mark's brief was 'any controllable load with a resolvable
        power sensor and a readable temperature'. Every gate is
        unchanged, including the temperature gate the fitter sits under
        without reading -- dropping that because it looks incidental
        would be a behaviour change smuggled in as a refactor."""
        head = self.body[: self._pos(_CHAIN_HEAD)]
        for gate in (
            "done_entity",
            "power_sensor",
            "done_condition.ATTRIBUTE_DONE_DOMAINS",
            "start_temperature is not None",
        ):
            with self.subTest(gate=gate):
                self.assertIn(gate, head, f"precondition {gate!r} was dropped")

    def test_it_is_inline_rather_than_a_top_level_helper(self):
        """Attempt 1's shape. A module-level helper cannot compile:
        `load_run_state`, `done_condition`, `thermal_forecast` and the
        `CONF_*` names are deferred imports local to this function, and
        this repo already knows that pattern is load-bearing (#735's own
        staging reversal)."""
        self.assertNotIn(
            "async def _async_sample_and_learn_thermal(",
            _SRC,
            "the top-level-helper shape was tried and does not compile "
            "-- five F821s from deferred imports local to "
            "apply_commanded_state_guard()",
        )


class TestTheStubGapThatHidItStaysClosed(unittest.TestCase):
    """Attempt 1 was backed out because 13 dispatch tests failed and the
    cause looked like a production regression. It was not: the stub HA
    did not export `entity_registry.async_entries_for_device`, which
    #768's discovery calls and #873 made reachable for every load kind.
    """

    def test_the_stub_exports_what_real_ha_does(self):
        stubs = (Path(__file__).resolve().parent / "_ha_stubs.py").read_text(
            encoding="utf-8"
        )
        block = re.search(
            r'module\(\s*"homeassistant\.helpers\.entity_registry".*?\n    \)',
            stubs,
            re.DOTALL,
        )
        self.assertIsNotNone(block, "the entity_registry stub moved -- re-read this")
        self.assertIn(
            "async_entries_for_device",
            block.group(0),
            "the entity_registry stub no longer exports "
            "async_entries_for_device, so #768's power-sensor discovery "
            "raises AttributeError inside the commanded-state guard and "
            "every load after it goes uncommanded, silently (nimbus "
            "#873 / #1019)",
        )


if __name__ == "__main__":
    unittest.main()
