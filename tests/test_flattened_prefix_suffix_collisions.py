"""Two flattened-attribute tables that share an `entity_id_prefix` must
not define the same `entity_id_suffix` (nimbus issue #1192 follow-up).

**Why this guard exists.** `sensor_flattened.py` has five
`create_flattened_entities_*` factories. Four own a prefix outright;
`nimbus_flex` is shared by two:

    create_flattened_entities_flex         -> nimbus_flex  (10 suffixes)
    create_flattened_entities_flex_report  -> nimbus_flex  ( 5 suffixes)

Both also attach to the same sub-device, `(DOMAIN, f"{entry_id}_flex")`,
deliberately -- "a genuine sibling grouping, not a second device", per
`create_flattened_entities_flex_report`'s own docstring.

Because a child's identity is built from prefix + suffix, those two
tables share one namespace. Today their suffix sets are disjoint, so
nothing collides. But nothing *enforces* that: adding a row to either
table with a suffix the other already uses would silently produce two
entities competing for one id, and HA would resolve it by bumping one to
`_2` -- exactly the class of duplicate this repo keeps rediscovering.

**This is a guard, not a fix for #1192.** The five collisions measured on
devhub on 2026-09-25 are all `FLATTENED_ATTRS_FLEX_REPORT` rows, and the
suffix overlap there is zero -- so a suffix clash is NOT their cause and
this test would not have caught them. It closes the adjacent hole while
that investigation continues, and it is the same enumeration problem
#1167 solved for the report's own fields and #1219/#1220 then hit again
for the publisher's: a rule held by convention rather than by a check
eventually stops holding.

The test DISCOVERS the tables and factories from the source rather than
listing them, so a sixth factory added later is covered automatically
without anyone remembering this file exists.
"""

from __future__ import annotations

import collections
import re
import sys
import unittest
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "sensor_flattened.py"
)


def _source() -> str:
    """`read_text`, not `open(...).read()`.

    The latter leaks the handle, and CI's pytest promotes the resulting
    ResourceWarning to an error -- so all five tests failed there while
    passing under a local `unittest` run, which does not. The regex and
    the assertions were never the problem.
    """
    return _SRC.read_text(encoding="utf-8")


def _factories(src: str) -> list[tuple[str, str, str]]:
    """(factory_name, entity_id_prefix, table_name) discovered from source."""
    return re.findall(
        r"def (create_flattened_entities_\w+)\(.*?"
        r'entity_id_prefix="([^"]+)".*?'
        r"for spec in (FLATTENED_ATTRS_\w+)",
        src,
        re.DOTALL,
    )


def _suffixes(src: str, table: str) -> set[str]:
    i = src.index(table + ":")
    j = src.index("\n)", i)
    return set(re.findall(r'entity_id_suffix="([^"]+)"', src[i:j]))


class TestTheDiscoveryItselfWorks(unittest.TestCase):
    """A guard built on a regex that silently matches nothing would pass
    forever while checking nothing. Pin the discovery before trusting it.
    """

    def test_it_finds_the_known_factories(self):
        facs = _factories(_source())
        self.assertGreaterEqual(
            len(facs),
            5,
            "expected at least the five known flattened factories; the "
            f"discovery regex found {len(facs)} -- it has probably stopped "
            "matching rather than the factories having gone away",
        )

    def test_every_discovered_table_yields_suffixes(self):
        src = _source()
        for name, _prefix, table in _factories(src):
            with self.subTest(factory=name, table=table):
                self.assertTrue(
                    _suffixes(src, table),
                    f"{table} parsed to zero suffixes -- the parse is "
                    "broken, not the table",
                )

    def test_the_known_shared_prefix_is_still_shared(self):
        """If this ever fails because `nimbus_flex` stopped being shared,
        that is good news and this test should be updated -- but it should
        be noticed, not silently skipped."""
        by_prefix = collections.defaultdict(list)
        for _name, prefix, table in _factories(_source()):
            by_prefix[prefix].append(table)
        shared = {p: t for p, t in by_prefix.items() if len(t) > 1}
        self.assertIn(
            "nimbus_flex",
            shared,
            "nimbus_flex was the only prefix shared by two tables when "
            f"this guard was written; shared prefixes now: {shared}",
        )


class TestNoSuffixCollisionWithinAPrefix(unittest.TestCase):
    """The actual invariant."""

    def test_tables_sharing_a_prefix_have_disjoint_suffixes(self):
        src = _source()
        by_prefix: dict[str, list[str]] = collections.defaultdict(list)
        for _name, prefix, table in _factories(src):
            by_prefix[prefix].append(table)

        for prefix, tables in sorted(by_prefix.items()):
            if len(tables) < 2:
                continue
            sets = {t: _suffixes(src, t) for t in tables}
            for a_i, a in enumerate(tables):
                for b in tables[a_i + 1 :]:
                    clash = sets[a] & sets[b]
                    with self.subTest(prefix=prefix, a=a, b=b):
                        self.assertEqual(
                            clash,
                            set(),
                            f"{a} and {b} both use entity_id_prefix "
                            f"'{prefix}' and both define {sorted(clash)} -- "
                            "those children would compete for one "
                            "entity_id and HA would bump one to _2. Give "
                            "one table its own prefix, or rename the "
                            "suffix.",
                        )

    def test_suffixes_are_unique_within_each_table_too(self):
        """A table that repeats a suffix collides with itself, which no
        amount of prefix separation fixes."""
        src = _source()
        for _name, _prefix, table in _factories(src):
            i = src.index(table + ":")
            j = src.index("\n)", i)
            found = re.findall(r'entity_id_suffix="([^"]+)"', src[i:j])
            dupes = [s for s, n in collections.Counter(found).items() if n > 1]
            with self.subTest(table=table):
                self.assertEqual(dupes, [], f"{table} repeats {dupes}")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
