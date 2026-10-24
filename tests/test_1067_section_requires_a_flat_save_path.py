"""nimbus #1067: a config-flow `section` cannot be added until the save
paths stop assuming flat `user_input`.

#1067 proposes surfacing the wizard's existing required/optional split
with Home Assistant's collapsible `section`s, and frames it as *"no
schema change and no change to what is stored"*. That is true of the
schema and **false of the save path**, and the gap between those two is
silent data loss on a live install.

## What a section does to `user_input`

Verified against the installed Home Assistant rather than recalled --
`homeassistant.data_entry_flow.section`::

    class section:
        def __call__(self, value: Any) -> Any:
            \"\"\"Validate input.\"\"\"
            return self.schema(value)

The section key validates a **dict** against the inner schema. So once
optional fields move inside one, `user_input` arrives nested::

    {"advanced": {"solver_charge_cost": 0.01, ...}, "solver_battery_capacity_kwh": 122.2}

## The two save patterns this breaks, differently

**1. `hub_options.py`'s flat key loop.**

    for key in _..._SCHEMA_KEYS:
        merged[key] = user_input.get(key)

`user_input.get("solver_charge_cost")` returns `None`, the loop writes
`None`, and `async_create_entry` persists it. On the Sources form that is
14 of 16 fields; across the wizard, **41 of 46**. Forecaster and
Switchboard have no required fields at all, so both would save entirely
blank.

That `.get()` rather than `in` is deliberate and load-bearing -- the
comment block above it records a real reported bug about an absent key
being indistinguishable from a cleared one -- so it cannot simply be
swapped.

**2. The subentry flows' wholesale `data=user_input`.**

Six subentry flows persist the submitted mapping directly. A section
there stores the **nested dict**, so every later `data["..."]` lookup
reads through a level that is not there.

Neither produces an exception. The flow completes, and the install comes
back configured with nulls or an unreadable shape -- the
confident-wrong-outcome shape rather than a crash, on the setup surface,
where a household has the least ability to diagnose it.

## What this test does

It does **not** forbid sections. It makes adding one fail loudly here,
naming the prerequisite, instead of failing silently on someone's
install. When the save paths are made section-aware, this test is the
thing to update -- deliberately, with the flattening in the same change.

Source-level for the same reason `test_1109`/`test_1111`'s equivalents
are: the defect is an absence, and driving it needs a real HA flow
render this suite cannot produce.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FLOW_DIR = REPO_ROOT / "custom_components" / "nimbus_load" / "flows"
CONFIG_FLOW = REPO_ROOT / "custom_components" / "nimbus_load" / "config_flow.py"

# The flat-key merge, verbatim. Both hub-options steps use it.
_FLAT_MERGE = "merged[key] = user_input.get(key)"
# The subentry flows' wholesale persist.
_WHOLESALE = "data=user_input"


def _flow_files() -> list[pathlib.Path]:
    files = [p for p in FLOW_DIR.glob("*.py") if p.name != "__init__.py"]
    if CONFIG_FLOW.exists():
        files.append(CONFIG_FLOW)
    return sorted(files)


def _imports_section(path: pathlib.Path) -> bool:
    """Whether this module imports HA's `section` helper.

    AST rather than a substring search: "section" appears in ordinary
    prose and attribute names throughout these files, and a guard that
    fires on a comment would be retired the first time it did.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and "data_entry_flow" in node.module
            and any(a.name == "section" for a in node.names)
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "section":
            # nimbus issue #1187 (Mark Purcell): walk an arbitrary-depth
            # attribute chain back to its root `ast.Name`, rather than
            # requiring exactly one level.
            #
            # The original `isinstance(value, ast.Name)` test only saw a
            # 2-level chain -- `data_entry_flow.section(...)` after
            # `from homeassistant import data_entry_flow`. A plain
            # `import homeassistant.data_entry_flow` followed by
            # `homeassistant.data_entry_flow.section(...)` puts another
            # `ast.Attribute` in `node.value`, so the guard returned
            # False and the section was invisible to it.
            #
            # That is ordinary Python, not a contrived shape. This
            # codebase happens to use `from X import Y` exclusively
            # today (zero `^import homeassistant.` hits), but nothing
            # enforces that, and a future flow file written the other
            # way would slip past the guard while genuinely triggering
            # the flat-save bug it exists to catch.
            parts = []
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
                if any("data_entry_flow" in part for part in parts):
                    return True
    return False


# nimbus #1067: hub_options.py is now section-aware and is the ONE allowed
# user of `section`. This guard was written to fire on exactly that change,
# and it did -- so it is narrowed here rather than deleted, because the
# reason it existed still applies everywhere else.
#
# WHAT MADE hub_options SAFE, and it is only safe for the SOLVER steps:
# `_absorb_step()` now calls `_flatten_section_input()` before merging and
# `_effective_schema_keys()` for its null sweep, so `self._solver_data` is
# flat. The Solver save loop reads THAT (`merged[key] = self._solver_data
# .get(key)`), never `user_input`, so a nested submission is already
# unwrapped by the time it is persisted.
#
# The Forecaster and Switchboard steps in the same file still read
# `user_input.get(key)` DIRECTLY, and the six subentry flows still persist
# `data=user_input` wholesale. Sectioning any of those would reproduce the
# original bug exactly, which is what the two tests below now pin.
_SECTION_ALLOWED = {"hub_options.py"}


class TestOnlyTheFlattenedPathUsesASection(unittest.TestCase):
    def test_no_unflattened_flow_module_imports_the_section_helper(self):
        offenders = [
            p.name
            for p in _flow_files()
            if _imports_section(p) and p.name not in _SECTION_ALLOWED
        ]
        self.assertEqual(
            offenders,
            [],
            f"{offenders} import Home Assistant's `section` helper while "
            "their save path still assumes flat `user_input`. A section "
            "nests its fields one level down, so the subentry flows' "
            f"`{_WHOLESALE}` stores the nested dict instead of the values, "
            "and a flat key loop writes None for every field inside it. "
            "Neither raises. Flatten that save path FIRST, as its own "
            "change with tests, then add the section and widen "
            "_SECTION_ALLOWED in the same commit -- see nimbus issue #1067.",
        )

    def test_hub_options_only_qualifies_because_it_flattens(self):
        """The allowance is conditional, not a permanent exemption. If the
        flattening were removed while the section stayed, this fails."""
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        for required in (
            "_flatten_section_input",
            "_effective_schema_keys",
            "merged[key] = self._solver_data.get(key)",
        ):
            with self.subTest(required=required):
                self.assertIn(
                    required,
                    text,
                    "hub_options.py uses a section, and this is what makes "
                    "that safe. Removing it reopens #1067's silent-null bug.",
                )

    def test_the_forecaster_and_switchboard_steps_are_NOT_sectioned(self):
        """Both still read `user_input.get(key)` directly, so neither may be
        wrapped. This is the specific regression the narrowing above could
        let through if nobody pinned it.

        Read from the source text rather than by importing the module: this
        file is a static guard over the flows directory and deliberately has
        no HA stubs installed.
        """
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        for step in ("async_step_forecaster", "async_step_switchboard"):
            i = text.index(f"async def {step}(")
            # the step body runs until the next method definition
            j = text.index(chr(10) + "    async def ", i + 10)
            with self.subTest(step=step):
                self.assertNotIn(
                    "_collapse_optionals_into_advanced",
                    text[i:j],
                    f"{step} saves via `user_input.get(key)`, so sectioning "
                    "it would write None for every optional field. Flatten "
                    "its save path first (#1067).",
                )


class TestTheGuardIsNotVacuous(unittest.TestCase):
    """If neither save pattern still exists, this guard is protecting
    nothing and should be re-read rather than left passing."""

    def test_the_flat_merge_is_still_there(self):
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        self.assertIn(
            _FLAT_MERGE,
            text,
            "hub_options.py no longer uses the flat-key merge this guard "
            "exists for. If the save path is now section-aware, relax the "
            "guard deliberately rather than leaving it asserting nothing.",
        )

    def test_the_flat_merge_is_used_by_both_steps(self):
        """Two steps share it. A fix that only reached one would leave the
        other silently vulnerable."""
        text = (FLOW_DIR / "hub_options.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count(_FLAT_MERGE), 2)

    def test_the_wholesale_persist_is_still_there(self):
        hits = [
            p.name for p in _flow_files() if _WHOLESALE in p.read_text(encoding="utf-8")
        ]
        self.assertTrue(
            hits,
            "no flow persists `data=user_input` wholesale any more -- the "
            "second half of this guard is protecting nothing.",
        )

    def test_there_are_flow_modules_to_check_at_all(self):
        """A rename or a move that emptied the glob would make every
        assertion above pass by measuring nothing."""
        self.assertGreater(len(_flow_files()), 5)


class TestTheDetectorItself(unittest.TestCase):
    """The detector is the part that can silently stop working."""

    def test_it_finds_a_from_import(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("from homeassistant.data_entry_flow import section\n")
            path = pathlib.Path(fh.name)
        self.assertTrue(_imports_section(path))

    def test_it_finds_an_attribute_use(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("import homeassistant.data_entry_flow as data_entry_flow\n")
            fh.write("x = data_entry_flow.section(schema)\n")
            path = pathlib.Path(fh.name)
        self.assertTrue(_imports_section(path))

    def test_prose_and_attribute_names_do_not_trip_it(self):
        """ "section" is an ordinary English word and appears throughout
        these files. A guard that fires on a comment gets retired."""
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("# see the section above about sections\n")
            fh.write('SECTION = "advanced"\n')
            fh.write('def section_title(): return "x"\n')
            path = pathlib.Path(fh.name)
        self.assertFalse(_imports_section(path))


if __name__ == "__main__":
    unittest.main()
