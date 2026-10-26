"""Coverage by risk: what each part of Nimbus is tested for (#1837, question 1).

#1837 asks "which modules are tested because they're genuinely risky
(dispatch-safety, real money) versus tested because a bug happened to land
there once". This answers it as a measurement, not prose:

1. Runs the stub suite (as CI's stub run selects it, `not slow_gate`) and
   `tests/hass_integration/` (real Home Assistant) under branch coverage
   with per-test contexts (`--cov-context=test`), plus every golden
   scenario.
2. Puts every module of `custom_components/nimbus_load` in one risk tier
   (RISK_TIERS below; a module matching none is an error).
3. For each module and tier, reports line and branch coverage and the share
   of its covered lines that ONLY issue pins reach (`test_<issue>_*.py`).
   A line only a pin reaches is covered because a bug landed there, not by
   a test written for what the module is meant to do.

    python scripts/coverage_by_risk.py --workers 14 \\
        --json docs/architecture/coverage-by-risk.json \\
        --md docs/architecture/coverage-by-risk.md

The tiers are a judgement, written down so they can be argued with: move a
row and rerun. The thresholds in FLAGS are the bar the report checks each
tier against; they are proposals, not CI gates.
"""

from __future__ import annotations

import argparse
import fnmatch
import heapq
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "custom_components/nimbus_load"

TIERS = {
    1: "Dispatch and money: decides what the battery, grid and loads do, or what is paid",
    2: "Forecasts and inputs the plan is built on",
    3: "Setup, configuration and the device contract",
    4: "Reports, dashboards and presentation",
}

# (glob relative to SOURCE, area, tier). First match wins.
RISK_TIERS: list[tuple[str, str, int]] = [
    # Tier 1 -- the LP and everything that turns its answer into commands.
    ("solver/lp.py", "solver (LP)", 1),
    ("solver/network.py", "solver (LP)", 1),
    ("solver/elements.py", "solver (LP)", 1),
    ("solver/p2p_export.py", "solver (LP)", 1),
    ("solver/cycle_lock.py", "solver (LP)", 1),
    ("solver/modes.py", "solver (LP)", 1),
    ("solver/pin_blockers.py", "solver (LP)", 1),
    ("solver/rolling.py", "solver (LP)", 1),
    ("solver/tracking.py", "solver (LP)", 1),
    ("solver/meter_reconciliation.py", "solver (LP)", 1),
    ("solver/stochastic.py", "solver (stochastic)", 1),
    ("solver/stochastic_dispatch.py", "solver (stochastic)", 1),
    ("solver/__init__.py", "solver (LP)", 1),
    ("solver_dispatch/*", "dispatch", 1),
    ("dispatch_rhs.py", "dispatch", 1),
    ("solver_plan.py", "solve cycle", 1),
    ("solver_writer.py", "solve cycle", 1),
    ("solver_runtime.py", "solve cycle", 1),
    ("solver_publish.py", "solve cycle", 1),
    ("p5min.py", "prices", 1),
    ("p5min_guard.py", "prices", 1),
    ("price_intervals.py", "prices", 1),
    ("pricing_source.py", "prices", 1),
    ("solver_inputs/prices.py", "prices", 1),
    ("solver_inputs/price_consolidation.py", "prices", 1),
    ("solver_inputs/price_freshness.py", "prices", 1),
    ("solver_inputs/price_error_history.py", "prices", 1),
    ("solver_inputs/battery_soc.py", "batteries and loads", 1),
    ("solver_inputs/battery_participants.py", "batteries and loads", 1),
    ("solver_inputs/extra_batteries.py", "batteries and loads", 1),
    ("solver_inputs/controllable_loads.py", "batteries and loads", 1),
    ("solver_inputs/controllable_load_history.py", "batteries and loads", 1),
    ("solver_inputs/calendar_trips.py", "batteries and loads", 1),
    ("solver_inputs/sign_convention.py", "batteries and loads", 1),
    ("solver_inputs/loss_factors.py", "batteries and loads", 1),
    ("solver_inputs/__init__.py", "batteries and loads", 1),
    ("done_condition.py", "batteries and loads", 1),
    ("load_run_state.py", "batteries and loads", 1),
    ("load_run_state_coordinator.py", "batteries and loads", 1),
    ("household_modes.py", "batteries and loads", 1),
    ("power_units.py", "batteries and loads", 1),
    # Live settings the solve reads every cycle (the number-reset bug lived here).
    ("number.py", "live solver settings", 1),
    ("switch.py", "live solver settings", 1),
    ("select.py", "live solver settings", 1),
    ("time.py", "live solver settings", 1),
    # Tier 2 -- forecasts and the inputs that feed the plan.
    ("ml/*", "load forecaster (ML)", 2),
    ("coordinator.py", "load forecaster (ML)", 2),
    ("thermal_forecast.py", "load forecaster (ML)", 2),
    ("anomaly.py", "load forecaster (ML)", 2),
    ("availability.py", "load forecaster (ML)", 2),
    ("device_forecasts.py", "load forecaster (ML)", 2),
    ("forecast_snapshot_store.py", "load forecaster (ML)", 2),
    ("solver_inputs/load.py", "solar and load inputs", 2),
    ("solver_inputs/solar*.py", "solar and load inputs", 2),
    ("solver_inputs/forecast_snapshot.py", "solar and load inputs", 2),
    ("solver/forecast_source_selection.py", "solar and load inputs", 2),
    ("nem_region.py", "price inputs", 2),
    ("aemo_crosscheck.py", "price inputs", 2),
    ("price_input_check.py", "input checks", 2),
    ("power_input_check.py", "input checks", 2),
    ("flex_telemetry.py", "input checks", 2),
    ("energy_prefs.py", "input checks", 2),
    # Tier 3 -- setup, configuration, device contract, HA plumbing.
    ("flows/*", "config flows", 3),
    ("config_flow.py", "config flows", 3),
    ("pricing_autodetect.py", "config flows", 3),
    ("known_sources.py", "config flows", 3),
    ("sensor_discovery.py", "config flows", 3),
    ("device_*.py", "device contract", 3),
    ("setup_builder.py", "setup", 3),
    ("setup_health.py", "setup", 3),
    ("readiness_sensor.py", "setup", 3),
    ("repairs.py", "setup", 3),
    ("health.py", "setup", 3),
    ("services.py", "HA plumbing", 3),
    ("__init__.py", "HA plumbing", 3),
    ("const.py", "HA plumbing", 3),
    ("solver_shared.py", "HA plumbing", 3),
    # Tier 4 -- what is shown, reported or recorded for later.
    ("sensor.py", "entities and cards", 4),
    ("sensor_*.py", "entities and cards", 4),
    ("solver_tab.py", "entities and cards", 4),
    ("help_tab.py", "entities and cards", 4),
    ("forecaster_dashboard.py", "entities and cards", 4),
    ("frontend.py", "entities and cards", 4),
    ("diagnostics.py", "entities and cards", 4),
    ("solver_reports/*", "quality and reports", 4),
    ("solver/quality_report.py", "quality and reports", 4),
    ("solver/regret.py", "quality and reports", 4),
    ("solver/forecast_regret.py", "quality and reports", 4),
    ("solver/counterfactuals.py", "quality and reports", 4),
    ("solver/backtest.py", "quality and reports", 4),
    ("solver/reference_benchmark.py", "quality and reports", 4),
    ("solver/nowcast_skill.py", "quality and reports", 4),
    ("solver/epr.py", "quality and reports", 4),
    ("forecast_stats.py", "quality and reports", 4),
    ("quality_history_store.py", "quality and reports", 4),
    ("decision_inputs.py", "quality and reports", 4),
    ("decision_inputs_store.py", "quality and reports", 4),
]

# The bar each tier is checked against (line %, branch %, max pin-only %).
FLAGS = {
    1: (90.0, 80.0, 20.0),
    2: (85.0, 70.0, 25.0),
    3: (80.0, 65.0, 30.0),
    4: (70.0, 50.0, 40.0),
}

PIN = re.compile(r"(^|/)test_\d+_[^/]*\.py$")


def modules() -> list[str]:
    base = ROOT / SOURCE
    return sorted(
        p.relative_to(base).as_posix()
        for p in base.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def tier_of(module: str) -> tuple[str, int] | None:
    for pattern, area, tier in RISK_TIERS:
        if fnmatch.fnmatchcase(module, pattern):
            return area, tier
    return None


def test_kind(context: str) -> str:
    """'pin', 'golden' or 'designed' for one coverage context."""
    if context.startswith("golden:"):
        return "golden"
    path = context.split("::", 1)[0].replace("\\", "/")
    return "pin" if PIN.search(path) else "designed"


def _run_suite(data: Path, workers: int) -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:homeassistant",
        "-p",
        "no:cacheprovider",
    ]
    if workers > 1:
        cmd += ["-n", str(workers)]
    cmd += [
        f"--cov={SOURCE}",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=",
        "tests/",
        "--ignore=tests/hass_integration/",
        "-m",
        "not slow_gate",
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["COVERAGE_FILE"] = str(data)
    return subprocess.run(
        cmd, cwd=ROOT, env=env, check=False, stdout=subprocess.DEVNULL
    ).returncode


def _run_hass(data: Path) -> int:
    """tests/hass_integration/, against real Home Assistant, as CI's second run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["COVERAGE_FILE"] = str(data)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--cov={SOURCE}",
            "--cov-branch",
            "--cov-context=test",
            "--cov-report=",
            "tests/hass_integration/",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        stdout=subprocess.DEVNULL,
    ).returncode


def _run_golden(data_dir: Path) -> list[Path]:
    sys.path.insert(0, str(ROOT / "tests"))
    from golden.harness import child_env
    from golden.scenarios import get, names

    out = []
    for name in names():
        part = data_dir / f".coverage.golden.{name}"
        with tempfile.TemporaryDirectory() as tmp:
            env = child_env(Path(tmp))
            env["COVERAGE_FILE"] = str(part)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    f"--source={SOURCE}",
                    f"--context=golden:{name}",
                    "-m",
                    "golden.child",
                    name,
                    str(Path(tmp) / "r.json"),
                    str(get(name).cycles),
                ],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                timeout=1800,
            )
        if part.exists():
            out.append(part)
    return out


def _key(path: str) -> str:
    return path.replace("\\", "/").split(f"{SOURCE}/")[-1]


def pin_only_lines(executed: list[int], contexts_by_line: dict[int, set[str]]) -> int:
    """How many of a module's executed statement lines only issue pins
    reach. Only the report's own executed lines count, so the result is
    never more than its covered lines: a line an arc names, or one measured
    under another copy's path only, is not a statement here."""
    return sum(
        1
        for line in executed
        if contexts_by_line.get(line)
        and {test_kind(c) for c in contexts_by_line[line]} == {"pin"}
    )


def pin_only_by_test(
    executed: list[int], contexts_by_line: dict[int, set[str]]
) -> dict[str, list[int]]:
    """The pin-only lines, by the issue-pin test file that reaches them: the
    lines a designed test has to reach before that module's pin-only share
    falls."""
    out: dict[str, set[int]] = {}
    for line in executed:
        ctxs = contexts_by_line.get(line)
        if not ctxs or {test_kind(c) for c in ctxs} != {"pin"}:
            continue
        for c in ctxs:
            out.setdefault(c.split("::", 1)[0], set()).add(line)
    return {k: sorted(v) for k, v in sorted(out.items())}


def measure(workers: int) -> tuple[dict, int, list[str], dict]:
    import coverage

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        suite = work / ".coverage.suite"
        # pytest exits 0 (passed) or 1 (some failed): both measured. Anything
        # else means the stage did not run (collection error, wrong Home
        # Assistant version), and a report without it would understate
        # whatever only that stage reaches.
        for stage, code in (
            ("stub suite", _run_suite(suite, workers)),
            ("real-HA suite", _run_hass(hass := work / ".coverage.hass")),
        ):
            if code not in (0, 1):
                raise SystemExit(f"{stage} did not run (pytest exit {code})")
        golden = _run_golden(work)
        sys.path.insert(0, str(ROOT / "tests"))
        from golden.scenarios import names

        if len(golden) != len(names()):
            raise SystemExit(
                f"golden: {len(golden)} of {len(names())} scenarios measured"
            )
        combined = work / ".coverage"
        merged = coverage.CoverageData(str(combined))
        for part in [suite, hass, *golden]:
            if part.exists():
                one = coverage.CoverageData(str(part))
                one.read()
                merged.update(one)
        merged.write()

        cov = coverage.Coverage(data_file=str(combined), branch=True, source=[SOURCE])
        cov.load()
        report = work / "report.json"
        cov.json_report(outfile=str(report), show_contexts=False)
        files = json.loads(report.read_text(encoding="utf-8"))["files"]

        data = cov.get_data()
        contexts = sorted(c for c in data.measured_contexts() if c)
        # Every context that reached each line, per module. One module can be
        # measured under more than one path (the real-HA suite runs a copy),
        # so contexts are merged by module, never overwritten by path.
        by_module: dict[str, dict[int, set[str]]] = {}
        for path in data.measured_files():
            lines = by_module.setdefault(_key(path), {})
            for line, ctxs in data.contexts_by_lineno(path).items():
                lines.setdefault(line, set()).update(c for c in ctxs if c)
        result = {}
        for path, f in files.items():
            s = f["summary"]
            pin_only = pin_only_lines(
                f["executed_lines"], by_module.get(_key(path), {})
            )
            result[_key(path)] = {
                "statements": s["num_statements"],
                "covered_lines": s["covered_lines"],
                "branches": s.get("num_branches", 0),
                "covered_branches": s.get("covered_branches", 0),
                "pin_only_lines": pin_only,
                "missing_lines": f.get("missing_lines", []),
                "missing_branches": f.get("missing_branches", []),
                "missing_pin_only_by_test": pin_only_by_test(
                    f["executed_lines"], by_module.get(_key(path), {})
                ),
            }
    return result, len(golden), contexts, by_module


def gaps(r: dict, measured: dict) -> dict:
    """The lines and branches no test reaches, for each flagged module: what
    a PR closing that module's gap has to cover. A branch is [from, to];
    a negative `to` is an exit from the function."""
    out = {}
    for row in r["modules"]:
        if not row["flags"]:
            continue
        m = measured.get(row["module"], {})
        out[row["module"]] = {
            "flags": row["flags"],
            "missing_lines": m.get("missing_lines", []),
            "missing_branches": m.get("missing_branches", []),
            "pin_only_by_test": m.get("missing_pin_only_by_test", {}),
        }
    return out


def test_of(context: str) -> str:
    """The test function a coverage context belongs to: its node id without
    parameters or the pytest phase (`|run`, `|setup`)."""
    return context.split("|", 1)[0].split("[", 1)[0]


def smallest_cover(by_module: dict[str, dict[int, set[str]]]) -> dict:
    """The fewest test functions that still reach every line the suite
    reaches (#1837: the household's ~1,000-test target). Greedy set cover:
    golden scenarios are always kept (they are not tests to retire); then,
    repeatedly, the test reaching the most lines not yet reached. Every
    other test reaches nothing the kept ones don't."""
    reach: dict[str, set[tuple[str, int]]] = {}
    for module, lines in by_module.items():
        for line, ctxs in lines.items():
            for c in ctxs:
                reach.setdefault(test_of(c), set()).add((module, line))
    golden = {t for t in reach if t.startswith("golden:")}
    covered: set[tuple[str, int]] = set()
    for t in golden:
        covered |= reach[t]
    tests = {t: s for t, s in reach.items() if t not in golden}
    # Lazy greedy: a test's gain only falls as lines are covered, so a
    # popped test whose fresh gain still beats the next stale one is best.
    heap = [(-len(s - covered), t) for t, s in tests.items()]
    heapq.heapify(heap)
    kept: list[str] = []
    while heap:
        _neg, t = heapq.heappop(heap)
        gain = len(tests[t] - covered)
        if gain == 0:
            continue
        if heap and gain < -heap[0][0]:
            heapq.heappush(heap, (-gain, t))
            continue
        kept.append(t)
        covered |= tests.pop(t)
    return {
        "tests_measured": len(kept) + len(tests),
        "kept": sorted(kept),
        "adds_nothing": sorted(tests),
        "lines": len(covered),
    }


def _pct(a: float, b: float) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def build(measured: dict) -> dict:
    rows, unmapped = [], []
    for m in modules():
        t = tier_of(m)
        if t is None:
            unmapped.append(m)
            continue
        area, tier = t
        d = measured.get(
            m,
            {
                "statements": 0,
                "covered_lines": 0,
                "branches": 0,
                "covered_branches": 0,
                "pin_only_lines": 0,
            },
        )
        line = _pct(d["covered_lines"], d["statements"])
        branch = _pct(d["covered_branches"], d["branches"])
        pin = _pct(d["pin_only_lines"], d["covered_lines"])
        lmin, bmin, pmax = FLAGS[tier]
        flags = []
        if d["statements"] and (line or 0) < lmin:
            flags.append(f"lines < {lmin:g}%")
        if d["branches"] and (branch or 0) < bmin:
            flags.append(f"branches < {bmin:g}%")
        if pin is not None and pin > pmax:
            flags.append(f"pin-only > {pmax:g}%")
        rows.append(
            {
                "module": m,
                "area": area,
                "tier": tier,
                # The missed lines go to --gaps, not into the report.
                **{k: v for k, v in d.items() if not k.startswith("missing_")},
                "line_pct": line,
                "branch_pct": branch,
                "pin_only_pct": pin,
                "flags": flags,
            }
        )
    if unmapped:
        raise SystemExit(f"modules with no risk tier: {unmapped}")

    def total(sel):
        keys = (
            "statements",
            "covered_lines",
            "branches",
            "covered_branches",
            "pin_only_lines",
        )
        t = {k: sum(r[k] for r in sel) for k in keys}
        t["line_pct"] = _pct(t["covered_lines"], t["statements"])
        t["branch_pct"] = _pct(t["covered_branches"], t["branches"])
        t["pin_only_pct"] = _pct(t["pin_only_lines"], t["covered_lines"])
        t["modules"] = len(sel)
        t["flagged"] = sum(1 for r in sel if r["flags"])
        return t

    tiers = {t: total([r for r in rows if r["tier"] == t]) for t in TIERS}
    areas = defaultdict(list)
    for r in rows:
        areas[(r["tier"], r["area"])].append(r)
    area_rows = [
        {"tier": t, "area": a, **total(rs)} for (t, a), rs in sorted(areas.items())
    ]
    return {"tiers": tiers, "areas": area_rows, "modules": rows}


def _f(v):
    return "–" if v is None else f"{v:.1f}"


def markdown(r: dict, meta: dict) -> str:
    out = [
        "# Coverage by risk",
        "",
        (
            'Generated by `scripts/coverage_by_risk.py` for #1837 (question 1: "a coverage map by risk, not by issue number"). '
            "Do not edit by hand: change `RISK_TIERS` or `FLAGS` in the script and rerun it."
        ),
        "",
        (
            f"Measured at `{meta['commit']}` on {meta['date']}: the stub suite (`not slow_gate`) and the real-HA suite, {meta['contexts']} test contexts, "
            f"plus {meta['golden']} golden scenarios, branch coverage, per-test contexts."
        ),
        "",
        "- **Line / branch %**: of the module's statements and branches, the share any test or golden scenario executes.",
        (
            "- **Pin-only %**: of the covered lines, the share that **only** issue pins (`test_<issue>_*.py`) reach. "
            "That code is covered because a bug landed there, not by a test written for what the module does."
        ),
        "- **Flags**: where a module misses its tier's bar (`FLAGS`). The bars are proposals for review, not CI gates.",
        "",
        "## Tiers",
        "",
        "| tier | what | modules | statements | line % | branch % | pin-only % | flagged |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for t, desc in TIERS.items():
        x = r["tiers"][t]
        out.append(
            f"| {t} | {desc} | {x['modules']} | {x['statements']} | {_f(x['line_pct'])} | "
            f"{_f(x['branch_pct'])} | {_f(x['pin_only_pct'])} | {x['flagged']} |"
        )
    out += [
        "",
        "Bars per tier (line % ≥, branch % ≥, pin-only % ≤): "
        + "; ".join(
            f"tier {t}: {a:g} / {b:g} / {c:g}" for t, (a, b, c) in FLAGS.items()
        ),
        "",
        "## Areas",
        "",
        "| tier | area | modules | statements | line % | branch % | pin-only % | flagged |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for a in r["areas"]:
        out.append(
            f"| {a['tier']} | {a['area']} | {a['modules']} | {a['statements']} | {_f(a['line_pct'])} | "
            f"{_f(a['branch_pct'])} | {_f(a['pin_only_pct'])} | {a['flagged']} |"
        )
    flagged = [m for m in r["modules"] if m["flags"]]
    flagged.sort(key=lambda m: (m["tier"], -(m["statements"] - m["covered_lines"])))
    out += [
        "",
        f"## Flagged modules ({len(flagged)})",
        "",
        "Ordered by tier, then by uncovered statements.",
        "",
        "| tier | module | statements | uncovered | line % | branch % | pin-only % | flags |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for m in flagged:
        out.append(
            f"| {m['tier']} | `{m['module']}` | {m['statements']} | {m['statements'] - m['covered_lines']} | "
            f"{_f(m['line_pct'])} | {_f(m['branch_pct'])} | {_f(m['pin_only_pct'])} | {'; '.join(m['flags'])} |"
        )
    out += [
        "",
        "## Every module",
        "",
        "| tier | area | module | statements | line % | branch % | pin-only % |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for m in sorted(r["modules"], key=lambda m: (m["tier"], m["area"], m["module"])):
        out.append(
            f"| {m['tier']} | {m['area']} | `{m['module']}` | {m['statements']} | {_f(m['line_pct'])} | "
            f"{_f(m['branch_pct'])} | {_f(m['pin_only_pct'])} |"
        )
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json")
    ap.add_argument("--md")
    ap.add_argument("--gaps", help="write each flagged module's missed lines")
    ap.add_argument("--cover", help="write the smallest covering set of tests")
    a = ap.parse_args()
    unmapped = [m for m in modules() if tier_of(m) is None]
    if unmapped:
        print(json.dumps({"error": "modules with no risk tier", "modules": unmapped}))
        return 2
    measured, golden, contexts, by_module = measure(a.workers)
    if not measured:
        print(json.dumps({"error": "coverage measured nothing"}))
        return 3
    commit = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    meta = {
        "commit": commit,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "golden": golden,
        "contexts": len(contexts),
    }
    r = build(measured)
    print(json.dumps({t: r["tiers"][t] for t in TIERS}))
    if a.json:
        Path(a.json).write_text(
            json.dumps({"meta": meta, **r}, indent=1) + "\n", encoding="utf-8"
        )
    if a.md:
        Path(a.md).write_text(markdown(r, meta), encoding="utf-8")
    if a.gaps:
        Path(a.gaps).write_text(
            json.dumps({"meta": meta, "modules": gaps(r, measured)}, indent=1) + "\n",
            encoding="utf-8",
        )
    if a.cover:
        Path(a.cover).write_text(
            json.dumps({"meta": meta, **smallest_cover(by_module)}, indent=1) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
