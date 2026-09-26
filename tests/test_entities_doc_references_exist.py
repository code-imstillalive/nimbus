"""Every entity_id the user-facing docs name must be one this integration
actually creates, and every unit `docs/entities.md` claims must match the
spec that produces it.

`README.md` has had this guard since nimbus issue #364
(`test_readme_entity_references_exist.py`). `docs/entities.md` — the
per-entity table a user reads to find out what exists — had none, and it
was wrong: it listed `sensor.nimbus_quality_uplift_available` as a real
entity with a unit and a meaning. That sensor was **deliberately
removed** under #283 defect 2, as a byte-identical duplicate of
`regret_dollars`, and `sensor_flattened.py` carries a comment saying so
directly above where the spec used to be. Confirmed three ways before
fixing: the composed spec set does not contain it, the removal comment
names it, and a live install returns 404 for it while
`sensor.nimbus_quality_regret_dollars` answers.

The same file's own defect list still recommended *making* that removal,
two sections below the table that presented it as shipped — so the doc
simultaneously claimed the entity existed and that it should be deleted.

Same failure mode as `solver/README.md`'s "not wired into anything"
claim (#364), which went unnoticed for months: a doc that is wrong in a
checkable way, with nothing checking it.

**Composing the real set is the whole difficulty**, and getting it wrong
in either direction makes this test worthless — too narrow and it fails
on real entities, too broad and it passes on phantoms. Two traps, both
hit while writing this:

- A flattened child's entity_id never appears as a literal anywhere. It
  is built at runtime from a family prefix plus the spec's own
  `entity_id_suffix`. A plain grep of the source reports 35 of the 45
  documented ids as missing; all but one are real.
- **The family prefix is not derivable from the tuple's name.**
  `FLATTENED_ATTRS_FLEX_REPORT` publishes under `nimbus_flex`, not
  `nimbus_flex_report`. Guessing produced five more false positives.

So the prefixes are read from the **instantiation sites**, never from the
tuple's name: the AST is walked for each comprehension that builds a
family, taking its explicit `entity_id_prefix=` where there is one and
falling back to the hub f-string for the families that keep the base
class's own id. Nothing here is a hardcoded mapping, so a new family is
picked up without editing this file — and
`test_every_flattened_family_is_accounted_for` fails if one ever appears
that this derivation cannot see. It did exactly that twice while this
file was being written, on `FLATTENED_ATTRS_CURRENT` and then
`FLATTENED_ATTRS_P2P`; both were real gaps in an earlier hardcoded
version, which is the best evidence the guard-on-the-guard earns its
place.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NIMBUS = _REPO_ROOT / "custom_components" / "nimbus_load"
_DOC = _REPO_ROOT / "docs" / "entities.md"
_FLATTENED_PY = _NIMBUS / "sensor_flattened.py"

# The user-facing docs. Deliberately NOT every .md in the repo:
#
# - `docs/worklog/*` is dated historical narrative. Several entity ids in
#   there were real when written and have since been removed, which is
#   the record doing its job — a guard over those would fight history.
# - `docs/real-world-integration/*` documents a standalone deployment
#   against one reference household, naming per-subentry ids a user
#   creates (`sensor.nimbus_test_hvac_power`). Those cannot be composed
#   from this source at all, so they are legitimately unverifiable rather
#   than stale.
# - `README.md` has its own guard (`test_readme_entity_references_exist`)
#   and deliberately shows `nimbus_load`-domain spellings while
#   explaining that naming quirk, which this check would misread.
_USER_FACING_DOCS = (
    "docs/entities.md",
    "docs/configuration-reference.md",
    "docs/dashboards.md",
    "docs/controllable-loads.md",
    "docs/setup-guide.md",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(_REPO_ROOT))
from custom_components.nimbus_load import sensor_flattened as sf

_ENTITY_RE = re.compile(
    r"\b(?:sensor|switch|number|select|binary_sensor)\.nimbus[a-z0-9_]*"
)


def _family_prefixes() -> dict[str, str]:
    """{FLATTENED_ATTRS_* name: entity_id_prefix}, read from the
    instantiation sites rather than guessed from the tuple's name — see
    this module's docstring for the `FLEX_REPORT`/`nimbus_flex` trap that
    makes guessing wrong.

    Both shapes are derived here, so no mapping is hardcoded:

    - a sub-device family passes its prefix explicitly as
      `entity_id_prefix=`;
    - a hub-device family (`_FlattenedAttributeSensor`) has no such
      keyword and builds the id from one f-string, which
      `_solver_family_prefix()` reads.

    An earlier version of this file hardcoded which tuples were which.
    `test_every_flattened_family_is_accounted_for` failed on
    `FLATTENED_ATTRS_CURRENT` the first time it ran — which is exactly
    what that guard exists to do, and the reason nothing here is a list
    someone has to remember to extend.
    """
    tree = ast.parse(_FLATTENED_PY.read_text(encoding="utf-8"))
    hub_prefix = _solver_family_prefix()
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            continue
        names = {
            n.id
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id.startswith("FLATTENED_ATTRS")
        }
        if len(names) != 1:
            continue
        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
        prefixes = {
            kw.value.value
            for c in calls
            for kw in c.keywords
            if kw.arg == "entity_id_prefix" and isinstance(kw.value, ast.Constant)
        }
        constructed = {c.func.id for c in calls if isinstance(c.func, ast.Name)}
        if len(prefixes) == 1:
            found[names.pop()] = prefixes.pop()
        elif any(c.startswith("_FlattenedAttributeSensor") for c in constructed):
            # A subclass that does NOT take entity_id_prefix keeps the
            # base class's own f-string, and therefore the hub prefix.
            # `_FlattenedAttributeSensorP2PSubDevice` is exactly this:
            # its docstring says it overrides only `_attr_device_info`,
            # deliberately leaving unique_id/entity_id alone so a
            # household's existing dashboard references keep working.
            # The prefix branch above already claims any subclass that
            # does re-derive the id.
            found[names.pop()] = hub_prefix
    return found


def _solver_family_prefix() -> str:
    """The Family-A prefix, read out of the f-string that builds it so a
    rename cannot silently strand this test on a stale value."""
    matches = re.findall(
        r'self\.entity_id = f"sensor\.([a-z0-9_]+)_\{spec\.entity_id_suffix\}"',
        _FLATTENED_PY.read_text(encoding="utf-8"),
    )
    assert len(matches) == 1, (
        f"expected exactly one Family-A entity_id site, got {matches}"
    )
    return matches[0]


def _documented_units() -> dict[str, str | None]:
    """{entity_id: unit} for every row of `docs/entities.md`'s own tables.

    `None` where the Unit column is a prose placeholder rather than a real
    unit — the table writes a dimensionless ratio as `(dimensionless
    0..1)`, which is the honest thing to show a reader and corresponds to
    a spec unit of `None`.
    """
    text = _DOC.read_text(encoding="utf-8")
    rows = re.findall(
        r"^\|\s*`([a-z_]+\.nimbus[a-z0-9_]*)`\s*\|\s*([^|]*?)\s*\|", text, re.MULTILINE
    )
    placeholders = {"(dimensionless 0..1)", "—", "-", ""}
    return {
        eid: (None if unit.strip() in placeholders else unit.strip())
        for eid, unit in rows
    }


# nimbus issue #1253: the currency rows hold SENTINELS, not units --
# `_FlattenedAttributeSensor.native_unit_of_measurement` resolves them
# against `hass.config.currency` at read time, so there is no single true
# string to document. `docs/entities.md` says "currency"/"currency/kWh" for
# exactly those rows, and this mapping is what keeps the assertion below a
# real check rather than a softened one: every non-currency unit is still
# compared literally, and a currency row that stopped being currency-driven
# (or a doc row that went back to claiming AUD) still fails.
_DOC_TOKEN_FOR_SENTINEL = {
    sf._CURRENCY: "currency",
    sf._CURRENCY_PER_KWH: "currency/kWh",
}


def _real_units() -> dict[str, str | None]:
    return {
        f"sensor.{prefix}_{spec.entity_id_suffix}": _DOC_TOKEN_FOR_SENTINEL.get(
            spec.unit_of_measurement, spec.unit_of_measurement
        )
        for tuple_name, prefix in _family_prefixes().items()
        for spec in getattr(sf, tuple_name)
    }


def _description_tuple_ids(module_name: str, domain: str) -> set[str]:
    """Entity ids composed from an EntityDescription-style tuple.

    `number.py` builds its ids as `f"number.nimbus_{desc.key}"`, so the
    full string exists nowhere in the source — the same shape as a
    flattened child, and the reason an earlier version of this test
    reported 36 of `configuration-reference.md`'s 41 number ids as
    phantom when every one of them was real.
    """
    mod = importlib.import_module(f"custom_components.nimbus_load.{module_name}")
    ids: set[str] = set()
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, tuple) and obj and hasattr(obj[0], "key"):
            ids.update(f"{domain}.nimbus_{desc.key}" for desc in obj)
    return ids


def _real_entity_ids() -> set[str]:
    real: set[str] = set(_real_units())
    real |= _description_tuple_ids("number", "number")

    # Every entity_id written as a literal anywhere in the integration —
    # the hub-level sensors, switches and selects that are not part of
    # any flattened family or description tuple.
    #
    # Honest weakness, stated rather than hidden: this matches literals in
    # COMMENTS too, so a comment naming a removed entity would make it
    # resolve. The `uplift_available` row this file was written for only
    # stayed detectable because `sensor_flattened.py`'s removal note names
    # the bare attribute, never the full entity_id. A composed source is
    # always preferred; this is the fallback for domains that have none.
    for path in _NIMBUS.rglob("*.py"):
        real.update(_ENTITY_RE.findall(path.read_text(encoding="utf-8")))
    return real


def _documented_entity_ids(path: Path | None = None) -> set[str]:
    return set(_ENTITY_RE.findall((path or _DOC).read_text(encoding="utf-8")))


class TestEntitiesDocNamesOnlyRealEntities(unittest.TestCase):
    def setUp(self):
        self.real = _real_entity_ids()
        self.documented = _documented_entity_ids()

    def test_the_derivation_found_a_plausible_number_of_entities(self):
        """A composition bug that produced an empty or tiny set would
        make the real assertion below fail for the wrong reason, or a
        runaway one would make it pass for the wrong reason."""
        self.assertGreater(len(self.real), 100)
        self.assertGreater(len(self.documented), 20)

    def test_every_documented_entity_id_is_real(self):
        for rel in _USER_FACING_DOCS:
            path = _REPO_ROOT / rel
            with self.subTest(doc=rel):
                self.assertTrue(path.exists(), f"{rel} is gone -- update this list")
                phantom = sorted(_documented_entity_ids(path) - self.real)
                self.assertEqual(
                    phantom,
                    [],
                    f"{rel} names these entity_ids, and this integration "
                    "creates none of them. A user reading that doc would "
                    "look for an entity that does not exist -- the exact "
                    "shape of nimbus issue #364. Either the entity was "
                    "removed and the reference should go (see "
                    "uplift_available, removed under #283), or it was "
                    "renamed and the reference should follow it.",
                )

    def test_every_documented_unit_matches_the_real_spec(self):
        """Names alone would not have caught #283's other two defects.
        This file's own Unit column is a claim about the entity, and it
        is checkable against the spec that produces it."""
        real = _real_units()
        documented = _documented_units()
        checked = 0
        for eid, doc_unit in sorted(documented.items()):
            if eid not in real:
                continue  # a non-flattened entity; names are covered above
            checked += 1
            with self.subTest(entity=eid):
                self.assertEqual(
                    doc_unit,
                    real[eid],
                    f"docs/entities.md says {eid} has unit {doc_unit!r}, its "
                    f"spec says {real[eid]!r}. #283's `tracking_fidelity` "
                    "defect was exactly this shape -- a unit that reads "
                    "plausibly and does not match the value.",
                )
        self.assertGreater(
            checked, 20, "sanity check: almost no documented units were compared"
        )

    def test_every_flattened_family_is_accounted_for(self):
        """The guard on the guard. If a new `FLATTENED_ATTRS_*` family is
        added and the AST derivation cannot see its prefix, its children
        silently drop out of `real` -- and this test would then start
        reporting real entities as phantom, or worse, stop covering
        them."""
        declared = {
            name
            for name in dir(sf)
            if name.startswith("FLATTENED_ATTRS")
            and isinstance(getattr(sf, name), tuple)
        }
        derived = set(_family_prefixes())
        unaccounted = declared - derived
        self.assertEqual(
            unaccounted,
            set(),
            f"{sorted(unaccounted)} declare flattened specs but this test "
            "could not find the entity_id_prefix they are instantiated "
            "with, so their children are missing from the real-entity set. "
            "Check how they are wired in sensor_flattened.py's setup.",
        )

    def test_the_flex_report_family_publishes_under_the_flex_prefix(self):
        """Pinned by name because it is the case that breaks the obvious
        implementation: the tuple is `FLATTENED_ATTRS_FLEX_REPORT` and the
        prefix is `nimbus_flex`. Anyone rewriting this derivation to use
        the tuple's own name will fail here rather than silently emit
        five phantom ids."""
        self.assertEqual(
            _family_prefixes()["FLATTENED_ATTRS_FLEX_REPORT"], "nimbus_flex"
        )

    def test_the_removed_uplift_sensor_stays_removed_from_the_doc(self):
        """The specific row this test was written for. `uplift_available`
        is still a real ATTRIBUTE on the quality report -- only the
        flattened entity went away -- so a future edit restoring the row
        is an easy mistake to make."""
        self.assertNotIn("sensor.nimbus_quality_uplift_available", self.documented)
        self.assertIn("sensor.nimbus_quality_regret_dollars", self.real)


if __name__ == "__main__":
    unittest.main()
