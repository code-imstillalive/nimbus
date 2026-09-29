"""`coverage_compare --moved-code` must match a line the extraction requalified.

## The defect (nimbus #1411)

The gate matches a base-covered line to its head counterpart by **line text**. Every
extraction phase from 2a onward *rewrites* the lines it moves: a moved body reaches
back into `solver_writer` as `sw.<name>` and reaches the shared logger as
`solver_shared._LOGGER`, because that deferred seam is how a lower-layer module reads
those without an upward import.

So the text stopped matching and the line read as uncovered. **The gate could not
verify the one operation it was built for.**

Measured on Phase 3 (#1302): **33 lines reported regressed, all 33 artifacts.**
`if _NATIVE_HASS is None:` occurred **0** times in the head tree while
`if sw._NATIVE_HASS is None:` occurred **4** — the code ran; the gate was looking for
a string that no longer existed. As written, the acceptance item in specs 003–007 was
unmeetable by any phase that qualifies a name, which is all of them.

## Why this file is separate from `test_gates_coverage_compare.py`

That file drives the gate end-to-end against real git worktrees with child pytest
runs — minutes per case, and measured at ~54 minutes per side on the reference
machine. `_match_key` is a pure string function and deserves a test that runs in
milliseconds, so the boundary cases below are actually cheap enough to enumerate
exhaustively.

## The negative cases are the point

A general "strip any `<name>.`" rule would flatten ordinary attribute access
(`plan.status` → `status`) and invent matches between unrelated lines — weakening the
key everywhere in order to fix it in two places. So the normalisation is narrow, and
most of what is asserted here is what it must leave **alone**.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "gates"))

from coverage_compare import _match_key


class TestRequalifiedLinesMatch(unittest.TestCase):
    """Each case is a real shape from Phase 2a/3's own diffs."""

    def test_the_four_forms_phase_3_actually_produced(self):
        for base, head in [
            ("    if _NATIVE_HASS is None:", "        if sw._NATIVE_HASS is None:"),
            (
                "    state_obj = _NATIVE_HASS.states.get(power_sensor)",
                "    state_obj = sw._NATIVE_HASS.states.get(power_sensor)",
            ),
            ("    _LOGGER.warning(", "    solver_shared._LOGGER.warning("),
            (
                "    return _period_index_for_instant(grid_times, target)",
                "    return sw._period_index_for_instant(grid_times, target)",
            ),
        ]:
            with self.subTest(head=head.strip()):
                self.assertEqual(
                    _match_key(base),
                    _match_key(head),
                    "a base line and the requalified head line it became must "
                    "produce the same matching key, or the gate reports a "
                    "coverage regression that did not happen (#1411)",
                )

    def test_an_unqualified_line_is_unchanged(self):
        """The overwhelming majority of lines are not touched by an extraction and
        must key to exactly their own stripped text."""
        for line, want in [
            ("    if _NATIVE_HASS is None:", "if _NATIVE_HASS is None:"),
            ("        plan = network.build_plan(", "plan = network.build_plan("),
            ("    return indices", "return indices"),
        ]:
            with self.subTest(line=line.strip()):
                self.assertEqual(_match_key(line), want)

    def test_ordinary_attribute_access_is_NOT_flattened(self):
        """The reason the normalisation is narrow. If `plan.status` keyed as
        `status`, two unrelated lines could match and a real regression could be
        waved through -- the one failure direction a gate must not have."""
        for line in [
            "    plan.status = 'optimal'",
            "    x = self.sw.thing",
            "    total = summary.solver_shared.count",
        ]:
            with self.subTest(line=line.strip()):
                self.assertEqual(_match_key(line), line.strip())

    def test_the_prefix_is_elided_only_at_an_identifier_boundary(self):
        """`sw.` inside a longer name is not a qualifier. Exhaustive over the
        character classes that can precede it."""
        for line in [
            "    y = xsw.thing",  # letter
            "    y = _sw.thing",  # underscore
            "    y = a.sw.thing",  # dot -- an attribute chain, not the seam
            "    y = 1sw.thing",  # digit
        ]:
            with self.subTest(line=line.strip()):
                self.assertEqual(_match_key(line), line.strip())

        for line, want in [
            ("    y = (sw.thing)", "y = (thing)"),
            ("    y = [sw.a, sw.b]", "y = [a, b]"),  # more than one on a line
            ("sw.thing()", "thing()"),  # at line start
            ("    y = {'k': sw.v}", "y = {'k': v}"),
        ]:
            with self.subTest(line=line.strip()):
                self.assertEqual(_match_key(line), want)

    def test_a_line_that_gains_a_trailing_comment_still_matches(self):
        """#1411's residual gap, and the last of Phase 3's 33 false positives.

        After the qualifier fix took 33 reported regressions to 1, the survivor
        was a line that gained a `# noqa` during the phase -- added because ruff
        cannot see a re-export's real consumers. Nothing about the line's
        execution changed, and it read as uncovered.
        """
        for base, head in [
            (
                "    from .solver import elements, lp, network",
                "    from .solver import elements, lp, network  # noqa: F401 -- re-export",
            ),
            (
                "        _REAFFIRM_CAP_WARNED,",
                "        _REAFFIRM_CAP_WARNED,  # noqa: F401 -- re-export (#1305)",
            ),
            (
                "            except Exception:",
                "            except Exception:  # noqa: BLE001 -- exc_info logged below",
            ),
        ]:
            with self.subTest(head=head.strip()[:48]):
                self.assertEqual(_match_key(base), _match_key(head))

    def test_a_hash_inside_a_string_is_NOT_treated_as_a_comment(self):
        """Why this uses `tokenize` and not `text.split("#")[0]`. Both of these
        would be truncated mid-literal by a regex or a naive split, silently
        making two different lines share a key."""
        for line in [
            '    url = "http://example/#frag"',
            '    x = "a # b"',
            "    s = '#'",
            '    parts = text.split("#")',
        ]:
            with self.subTest(line=line.strip()):
                self.assertEqual(_match_key(line), line.strip())

    def test_a_hash_in_a_string_plus_a_real_comment_keeps_the_string(self):
        """The case that distinguishes a correct implementation from one that
        merely passes the two tests above separately."""
        self.assertEqual(_match_key('    x = "a # b"  # real comment'), 'x = "a # b"')

    def test_it_fails_open_on_a_fragment_tokenize_cannot_parse(self):
        """`_match_key` runs on individual PHYSICAL lines, and a fragment of a
        multi-line expression is not always a valid token stream. Returning it
        unchanged is exactly the old behaviour, so a parse failure is neutral
        rather than a new way to break the gate."""
        for line in ["        elements,", "    )", "    ]", "        **kwargs,"]:
            with self.subTest(line=line.strip()):
                self.assertEqual(_match_key(line), line.strip())

    def test_the_two_normalisations_compose(self):
        """A requalified line that ALSO gained a comment -- the shape a real
        extraction phase produces, since both happen in the same edit."""
        self.assertEqual(
            _match_key("    _LOGGER.warning("),
            _match_key("    solver_shared._LOGGER.warning(  # noqa: G004"),
        )

    def test_the_prefix_list_is_not_silently_empty(self):
        """Non-vacuity. If `_QUALIFIER_PREFIXES` were emptied, every assertion
        above about elision would fail, but a reader skimming this file could
        believe it still guards something."""
        from coverage_compare import _QUALIFIER_PREFIXES

        self.assertIn("sw.", _QUALIFIER_PREFIXES)
        self.assertIn("solver_shared.", _QUALIFIER_PREFIXES)

    def test_failing_to_list_a_future_alias_degrades_toward_false_positives(self):
        """Stated as a test because it is the property that makes an incomplete
        list tolerable: an unlisted alias is NOT elided, so the line fails to
        match and is reported as a regression. Noisy, never silent -- which is
        the right direction for a gate to fail in."""
        unlisted = "    if newalias._NATIVE_HASS is None:"
        base = "    if _NATIVE_HASS is None:"
        self.assertNotEqual(
            _match_key(unlisted),
            _match_key(base),
            "an unlisted qualifier must NOT match, so the failure is a visible "
            "false positive rather than a missed regression",
        )


if __name__ == "__main__":
    unittest.main()
