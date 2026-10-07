"""Spec DC-000 step 2: measure the producer/consumer/write-boundary inventory
of the integration at a pinned commit.

    python scripts/device_contract_baseline.py <repo-root> > inventory.json

Measured from the code by parsing it (Python `ast`, plus literal scans of the
bundled cards), never written by hand, so the inventory cannot drift from
the code it describes. It records:

- **config keys**: every `CONF_*` in const.py, and which modules read it, by
  constant name or by its string value (the Solver reads `cfg.get("...")`);
- **families**: which consumer family each reading module belongs to
  (forecaster, solver, scoring, setup, topology, cards, platform);
- **write boundaries**: options/subentry writes, Repairs, notifications,
  published states and service calls, with the calling module and line;
- **control-capable calls**: service calls whose domain can actuate
  equipment (switch, number, select, script, climate, ...);
- **standalone paths**: modules with an `except ImportError` fallback, the
  cron/standalone deployment that the native HA path does not exercise.

Deterministic: sorted output, no timestamps, so two runs on one commit are
byte-identical (DC-000 "Repeatability").
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PKG = Path("custom_components") / "nimbus_load"

# Module path prefix -> consumer family. First match wins.
FAMILIES: tuple[tuple[str, str], ...] = (
    ("frontend/nimbus-topology-card", "topology"),
    ("frontend/", "cards"),
    ("flows/", "setup"),
    ("setup_", "setup"),
    ("pricing_autodetect", "setup"),
    ("power_input_check", "setup"),
    ("device_resolver", "setup"),
    ("energy_prefs", "setup"),
    ("forecaster_dashboard", "cards"),
    ("solver_reports/", "scoring"),
    ("solver/quality_report", "scoring"),
    ("solver/regret", "scoring"),
    ("solver/epr", "scoring"),
    ("solver/meter_reconciliation", "scoring"),
    ("solver/", "solver"),
    ("solver_", "solver"),
    ("coordinator", "forecaster"),
    ("ml/", "forecaster"),
    ("load_run_state", "forecaster"),
)

WRITE_CALLS = {
    "async_update_entry": "config_options_write",
    "async_add_subentry": "subentry_write",
    "async_update_subentry": "subentry_write",
    "async_remove_subentry": "subentry_write",
    "async_create_issue": "repair_issue",
    "async_delete_issue": "repair_issue",
    "async_set": "state_publish",
    "ha_post_state": "state_publish",
    "async_save": "storage_write",
}

CONTROL_DOMAINS = {
    "switch",
    "number",
    "select",
    "script",
    "climate",
    "input_number",
    "input_select",
    "input_boolean",
    "button",
    "water_heater",
    "light",
    "fan",
    "cover",
    "lock",
    "modbus",
}


def family(rel: str) -> str:
    for prefix, name in FAMILIES:
        if rel.startswith(prefix):
            return name
    return "platform"


def config_keys(root: Path) -> dict[str, str]:
    tree = ast.parse((root / PKG / "const.py").read_text(encoding="utf-8"))
    keys: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            for t in targets:
                if (
                    isinstance(t, ast.Name)
                    and t.id.startswith("CONF_")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    keys[t.id] = node.value.value
    return keys


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _wrappers(root: Path) -> set[str]:
    """Functions that pass a domain argument straight into a service call
    (e.g. solver_writer.ha_call_service_with_response). Their call sites are
    where the real domain is, so those are scanned as service calls."""
    names: set[str] = set()
    for path in sorted((root / PKG).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = {a.arg for a in fn.args.args}
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and _call_name(node) in ("async_call", "call_service")
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in params
                ):
                    names.add(fn.name)
    return names


def _calls_enclosed_by_wrappers(tree: ast.AST, wrappers: set[str]) -> set[int]:
    """`id()` of every `ast.Call` node lexically inside a function named in
    `wrappers` (nimbus #1624, IV&V pass). Scoped per function, not "anywhere
    a wrapper exists anywhere in the codebase" -- the pass-through exclusion
    below is only ever correct for a call site that is actually inside one of
    these functions."""
    ids: set[int] = set()
    for fn in ast.walk(tree):
        if (
            isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            and fn.name in wrappers
        ):
            ids.update(id(n) for n in ast.walk(fn) if isinstance(n, ast.Call))
    return ids


def scan_python(root: Path, keys: dict[str, str]):
    by_value = {v: k for k, v in keys.items()}
    readers: dict[str, set[str]] = defaultdict(set)
    writes: list[dict] = []
    services: list[dict] = []
    standalone: set[str] = set()
    wrappers = _wrappers(root)
    for path in sorted((root / PKG).rglob("*.py")):
        rel = path.relative_to(root / PKG).as_posix()
        if rel == "const.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosed_by_wrapper = _calls_enclosed_by_wrappers(tree, wrappers)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in keys:
                readers[node.id].add(rel)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in by_value:
                    readers[by_value[node.value]].add(rel)
            elif isinstance(node, ast.ExceptHandler):
                t = node.type
                if isinstance(t, ast.Name) and t.id == "ImportError":
                    standalone.add(rel)
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name in WRITE_CALLS:
                    writes.append(
                        {
                            "module": rel,
                            "line": node.lineno,
                            "call": name,
                            "kind": WRITE_CALLS[name],
                        }
                    )
                if name in ("async_call", "call_service", *wrappers) and node.args:
                    first = node.args[0]
                    if (
                        name in ("async_call", "call_service")
                        and isinstance(first, ast.Name)
                        and id(node) in enclosed_by_wrapper
                    ):
                        # The wrapper's own pass-through call: its call
                        # sites carry the real domains (recorded there).
                        continue
                    domain = first.value if isinstance(first, ast.Constant) else None
                    svc = (
                        node.args[1].value
                        if (
                            len(node.args) > 1
                            and isinstance(node.args[1], ast.Constant)
                        )
                        else None
                    )
                    services.append(
                        {
                            "module": rel,
                            "line": node.lineno,
                            "domain": domain
                            if isinstance(domain, str)
                            else "<dynamic>",
                            "service": svc if isinstance(svc, str) else "<dynamic>",
                            "control_capable": isinstance(domain, str)
                            and domain in CONTROL_DOMAINS,
                            "via": name if name in wrappers else None,
                        }
                    )
    return readers, writes, services, standalone


def scan_cards(root: Path, keys: dict[str, str]) -> dict[str, set[str]]:
    readers: dict[str, set[str]] = defaultdict(set)
    for path in sorted((root / PKG / "frontend").glob("*.js")):
        rel = path.relative_to(root / PKG).as_posix()
        text = path.read_text(encoding="utf-8")
        for name, value in keys.items():
            if re.search(r"\b" + re.escape(value) + r"\b", text):
                readers[name].add(rel)
    return readers


def inventory(root: Path) -> dict:
    keys = config_keys(root)
    py_readers, writes, services, standalone = scan_python(root, keys)
    js_readers = scan_cards(root, keys)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    config = {}
    for name in sorted(keys):
        mods = sorted(py_readers.get(name, set()) | js_readers.get(name, set()))
        config[name] = {
            "key": keys[name],
            "read_by": mods,
            "families": sorted({family(m) for m in mods}),
        }
    multi = sorted(
        n for n, c in config.items() if len(set(c["families"]) - {"platform"}) > 1
    )
    unread = sorted(n for n, c in config.items() if not c["read_by"])
    return {
        "baseline": commit,
        "summary": {
            "config_keys": len(config),
            "config_keys_read_by_more_than_one_family": len(multi),
            "config_keys_never_read": len(unread),
            "write_boundaries": len(writes),
            "service_calls": len(services),
            "control_capable_service_calls": sum(
                s["control_capable"] for s in services
            ),
            "dynamic_domain_service_calls": sum(
                s["domain"] == "<dynamic>" for s in services
            ),
            "modules_with_standalone_fallback": len(standalone),
        },
        "config": config,
        "shared_across_families": multi,
        "never_read": unread,
        "writes": sorted(writes, key=lambda w: (w["module"], w["line"])),
        "service_calls": sorted(services, key=lambda s: (s["module"], s["line"])),
        "standalone_fallback_modules": sorted(standalone),
    }


if __name__ == "__main__":
    print(
        json.dumps(
            inventory(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")),
            indent=1,
            sort_keys=False,
        )
    )
