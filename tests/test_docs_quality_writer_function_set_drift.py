"""The quality writer has the same two-copy drift risk as the forecast
writer, and nothing was watching it.

nimbus issue #937 exposed this. That issue's own top-priority action is "more
days" of the day-ahead `forecast_regret` decomposition — and there are none,
because the sub-dict is assembled **only** in
`docs/real-world-integration/files/nimbus_solver_quality_writer.py:1116` and
has never existed in the integration (`git log -S` over `custom_components/`
returns no commits for the published key).

So the metric that issue rests on stopped when the reference household moved to
the native path, and no HACS install has ever produced it.

## Why nothing caught it

`test_docs_writer_function_set_drift.py` guards exactly one pair:

    custom_components/nimbus_load/solver_writer.py
    docs/real-world-integration/files/nimbus_solver_forecast_writer.py

The **quality** writer is a second, independent two-copy pair with the same
failure mode, and it had no guard at all. A whole published attribute could
exist in one copy and not the other indefinitely, which is what happened.

## What this measured when it was written

13 top-level functions exist in the cron quality writer and nowhere in the
integration. Two of them are the direct cause of #937's gap:

* `load_forecast_snapshot` — loads the real day-ahead Solver forecast snapshot
  that `compute_forecast_regret()` needs;
* `resample_snapshot_to_grid` — puts it on the solve grid.

Without those the native path cannot compute the day-ahead decomposition at
all, which is the concrete blocker #937 was missing.

The rest split into two honest categories, and the lists below say which is
which rather than lumping them together as "drift".

## This test does not demand parity

Some of these differences are correct and permanent — the cron copy persists
its history to a JSON file because a cron process has nowhere else to put it,
while the integration keeps it in an entity attribute. Forcing those to match
would be worse than the drift.

What it demands is that every difference is **named**, so the next one is a
decision rather than an accident.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DOCS_QUALITY = (
    _ROOT
    / "docs"
    / "real-world-integration"
    / "files"
    / "nimbus_solver_quality_writer.py"
)
# The integration's quality surface is the UNION across the modules that
# together replace the cron writer -- same reasoning as the sibling guard's
# own union over the #735 extractions: relocating a function inside the
# integration must not read as removing it.
_INTEGRATION_FILES = (
    _ROOT / "custom_components" / "nimbus_load" / "solver" / "quality_report.py",
    _ROOT / "custom_components" / "nimbus_load" / "solver_publish.py",
    _ROOT / "custom_components" / "nimbus_load" / "solver_writer.py",
    _ROOT / "custom_components" / "nimbus_load" / "solver" / "forecast_regret.py",
    _ROOT / "custom_components" / "nimbus_load" / "solver" / "nowcast_skill.py",
)


# Real execution-context differences. The cron process has no `hass`, no entity
# attributes and no recorder, so it has to do by hand what the integration gets
# from Home Assistant. These are correct and permanent, not a backlog.
INTENTIONAL_CRON_ONLY = frozenset(
    {
        # The cron copy persists the quality history to a JSON file because a
        # process that exits between runs has nowhere else to keep it. The
        # integration keeps it on the entity attribute (see #1248 for what
        # that costs when a read degrades).
        "load_quality_history",
        "save_quality_history",
        # No recorder access without `hass`; the integration calls HA's own.
        "fetch_history_range",
        # Hand-rolled resampling/lookup the integration gets from its own
        # shared helpers rather than duplicating here.
        "resample_nearest_float",
        "resample_nearest_str",
        "value_at_or_before",
        "robust_value_near",
        "num",
    }
)


# Genuine, still-unported gaps. Each one is a capability the standalone copy
# has and the native path does not, with a real consequence named.
KNOWN_OPEN_DRIFT_CRON_ONLY = frozenset(
    {
        # nimbus issue #937, and the direct cause of it. These two load and
        # grid-align the real day-ahead Solver forecast snapshot that
        # `compute_forecast_regret()` needs. Without them the integration
        # cannot compute the day-ahead decomposition, which is why that
        # issue's 14-day sample stopped when the reference household moved to
        # the native path and why no HACS install has ever produced one.
        #
        # `compute_forecast_regret()` itself IS in the integration -- its only
        # callers are #919's one-step-ahead `nowcast_skill.py` and
        # `run_reference_benchmark()`, which nothing calls. So the machinery
        # is present and the INPUT is what is missing.
        "load_forecast_snapshot",
        "resample_snapshot_to_grid",
        # Pricing helpers the cron copy needs to price the two counterfactual
        # scenarios. Listed with the two above because they are part of the
        # same unported block, not separately interesting.
        "flat_p2p_rate_for_day",
        "network_energy_rate",
        "battery_discharge_cost_rate",
    }
)


def _top_level_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _integration_defs() -> set[str]:
    out: set[str] = set()
    for path in _INTEGRATION_FILES:
        if path.exists():
            out |= _top_level_defs(path)
    return out


class TestTheGuardIsLookingAtRealFiles(unittest.TestCase):
    """A silent zero on either side would make every assertion below pass
    vacuously -- the same sanity check the sibling guard makes."""

    def test_both_sides_have_real_content(self):
        self.assertGreater(len(_top_level_defs(_DOCS_QUALITY)), 10)
        self.assertGreater(len(_integration_defs()), 40)

    def test_every_integration_file_exists(self):
        missing = [p.name for p in _INTEGRATION_FILES if not p.exists()]
        self.assertEqual(
            missing,
            [],
            "this guard's union is stale -- a file it names has moved or been "
            "renamed, which would silently shrink the integration's side and "
            "report real parity as drift",
        )


class TestQualityWriterDriftIsAccountedFor(unittest.TestCase):
    def test_every_cron_only_function_is_classified(self):
        cron_only = _top_level_defs(_DOCS_QUALITY) - _integration_defs()
        unaccounted = cron_only - INTENTIONAL_CRON_ONLY - KNOWN_OPEN_DRIFT_CRON_ONLY
        self.assertEqual(
            unaccounted,
            set(),
            f"{sorted(unaccounted)} exist in the standalone quality writer "
            f"(docs/real-world-integration/files/nimbus_solver_quality_writer.py) "
            f"but nowhere in the integration, and are in neither list in this "
            f"file. Decide which: INTENTIONAL_CRON_ONLY for a real "
            f"execution-context difference (no hass, no recorder, no entity "
            f"attributes), or KNOWN_OPEN_DRIFT_CRON_ONLY for a capability the "
            f"native path genuinely lacks -- and say what the consequence is. "
            f"nimbus issue #937 is what an unnamed one costs.",
        )


class TestTheListsDoNotGoStale(unittest.TestCase):
    """A name that stops existing in the cron copy must not keep sitting in a
    list here, reporting a resolved gap as if it were still open -- or a
    permanent difference as if it still existed."""

    def test_no_listed_name_has_vanished_from_the_cron_copy(self):
        cron = _top_level_defs(_DOCS_QUALITY)
        for name, label in (
            *((n, "INTENTIONAL_CRON_ONLY") for n in INTENTIONAL_CRON_ONLY),
            *((n, "KNOWN_OPEN_DRIFT_CRON_ONLY") for n in KNOWN_OPEN_DRIFT_CRON_ONLY),
        ):
            with self.subTest(name=name, list=label):
                self.assertIn(
                    name,
                    cron,
                    f"{name} is listed in {label} but no longer exists in the "
                    "standalone quality writer -- remove it from the list "
                    "rather than leaving a stale annotation",
                )

    def test_no_listed_name_has_quietly_been_ported(self):
        """The more interesting direction: if a KNOWN_OPEN_DRIFT entry now
        exists in the integration, the gap is CLOSED and the list is lying."""
        integration = _integration_defs()
        ported = sorted(KNOWN_OPEN_DRIFT_CRON_ONLY & integration)
        self.assertEqual(
            ported,
            [],
            f"{ported} now exist in the integration, so they are no longer "
            f"open drift. Remove them from KNOWN_OPEN_DRIFT_CRON_ONLY -- and "
            f"if that list empties of the #937 entries, say so on that issue, "
            f"because it means the day-ahead decomposition became computable "
            f"on the native path.",
        )


class TestThe937GapIsPinnedSpecifically(unittest.TestCase):
    """Not a general assertion: this is the specific thing #937 needs, named
    so that closing it is visible rather than incidental."""

    def test_the_forecast_snapshot_loader_is_still_cron_only(self):
        integration = _integration_defs()
        self.assertNotIn(
            "load_forecast_snapshot",
            integration,
            "load_forecast_snapshot now exists in the integration -- the "
            "day-ahead forecast_regret decomposition may now be computable "
            "there. Update nimbus issue #937, whose top-priority action "
            "('more days') has been blocked on exactly this.",
        )

    def test_compute_forecast_regret_IS_available_so_only_the_input_is_missing(self):
        """The distinction that makes this tractable: the machinery is
        already native, only its input is not."""
        self.assertIn("compute_forecast_regret", _integration_defs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
