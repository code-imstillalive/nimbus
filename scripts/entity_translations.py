"""Entity names as translations (HA quality scale `entity-translations`, #2392).

Every Nimbus entity name lives in strings.json under `entity.<platform>.<key>.name`
and the entity carries `_attr_translation_key`. The English text is the same text
the entity used to set as `_attr_name`, so an entity id Home Assistant derives
from a name comes out unchanged.

Read from the source with `ast`, no Home Assistant needed:

- the description tables in number.py (`_DESCRIPTIONS`,
  `_CONTROLLABLE_LOAD_DESCRIPTIONS`: key, name);
- every `FlattenedAttrSpec(...)` in sensor_flattened.py (entity_id_suffix, name);
- every `NimbusSolverSwitch(...)` / `NimbusSelect(...)` call (key, name);
- `TIME_SETTINGS` in time.py (key, name);
- every literal `_attr_translation_key = "..."` (its name is whatever strings.json holds).

    python scripts/entity_translations.py --check   # exit 1 if strings.json is out of step
    python scripts/entity_translations.py --write   # rewrite the entity block of both files
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
STRINGS = PKG / "strings.json"
EN = PKG / "translations" / "en.json"

PLATFORM_OF_FILE = {
    "number.py": "number",
    "select.py": "select",
    "switch.py": "switch",
    "time.py": "time",
    "sensor.py": "sensor",
    "sensor_flattened.py": "sensor",
    "sensor_p5min.py": "sensor",
    "readiness_sensor.py": "sensor",
}
KEY_RE = re.compile(r"^[a-z0-9_-]+$")


def slug(name: str) -> str:
    """The key a plain English name became (#2392's one-off conversion used this)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


def _constants() -> dict[str, str]:
    """Module-level `NAME = "text"` in const.py and the platform modules."""
    out: dict[str, str] = {}
    for f in [PKG / "const.py", *(PKG / n for n in PLATFORM_OF_FILE)]:
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            value = getattr(node, "value", None)
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for t in targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = value.value
    return out


def _text(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return None
    return None


def _arg(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return call.args[index] if len(call.args) > index else None


def collect() -> tuple[dict[str, dict[str, str]], list[tuple[str, str, str]]]:
    """({platform: {key: English name}} from the tables, [(platform, key, file)] of literal keys)."""
    consts = _constants()
    named: dict[str, dict[str, str]] = {}
    literal: list[tuple[str, str, str]] = []

    def add(platform: str, key: str | None, name: str | None, where: str) -> None:
        if key is None or name is None:
            raise SystemExit(f"{where}: could not read key/name")
        have = named.setdefault(platform, {})
        if have.get(key, name) != name:
            raise SystemExit(f"{platform}.{key}: two names, {have[key]!r} and {name!r}")
        have[key] = name

    for fname, platform in PLATFORM_OF_FILE.items():
        tree = ast.parse((PKG / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", None)
                )
                if fn in (
                    "_SolverNumberDescription",
                    "_ControllableLoadNumberDescription",
                ):
                    add(
                        platform,
                        _text(_arg(node, 0, "key"), consts),
                        _text(_arg(node, 1, "name"), consts),
                        fname,
                    )
                elif fn == "FlattenedAttrSpec":
                    add(
                        platform,
                        _text(_arg(node, 99, "entity_id_suffix"), consts),
                        _text(_arg(node, 99, "name"), consts),
                        fname,
                    )
                elif fn in ("NimbusSolverSwitch", "NimbusSelect"):
                    add(
                        platform,
                        _text(_arg(node, 1, "key"), consts),
                        _text(_arg(node, 2, "name"), consts),
                        fname,
                    )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for t in targets:
                    if (
                        isinstance(t, ast.Name)
                        and t.id == "TIME_SETTINGS"
                        and isinstance(node.value, ast.Tuple)
                    ):
                        for el in node.value.elts:
                            add(
                                platform,
                                _text(el.elts[0], consts),
                                _text(el.elts[1], consts),
                                fname,
                            )
                    attr = (
                        t.attr
                        if isinstance(t, ast.Attribute)
                        else t.id
                        if isinstance(t, ast.Name)
                        else None
                    )
                    if attr == "_attr_translation_key" and isinstance(
                        node.value, ast.Constant
                    ):
                        literal.append((platform, node.value.value, fname))
    return named, literal


def remaining_attr_names() -> list[str]:
    """Every `_attr_name =` left in the package (there should be none)."""
    out = []
    for f in sorted(PKG.rglob("*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b_attr_name\s*[:=]", line) and not line.lstrip().startswith(
                "#"
            ):
                out.append(f"{f.relative_to(PKG)}:{i}")
    return out


def expected_block(existing: dict) -> dict[str, dict[str, dict[str, str]]]:
    named, literal = collect()
    block: dict[str, dict[str, dict[str, str]]] = {}
    for platform, keys in named.items():
        for key, name in keys.items():
            block.setdefault(platform, {})[key] = {"name": name}
    for platform, key, fname in literal:
        name = existing.get(platform, {}).get(key, {}).get("name")
        if name is None:
            raise SystemExit(
                f"{fname}: translation key {platform}.{key} has no name in strings.json"
            )
        block.setdefault(platform, {})[key] = {"name": name}
    for platform, keys in block.items():
        for key in keys:
            if not KEY_RE.match(key):
                raise SystemExit(f"{platform}.{key}: not a valid translation key")
    return {p: dict(sorted(k.items())) for p, k in sorted(block.items())}


def write(strings: dict | None = None) -> None:
    strings = strings or json.loads(STRINGS.read_text(encoding="utf-8"))
    strings["entity"] = expected_block(strings.get("entity", {}))
    text = json.dumps(strings, indent=2, ensure_ascii=False) + "\n"
    STRINGS.write_text(text, encoding="utf-8")
    EN.write_text(text, encoding="utf-8")


def check() -> list[str]:
    strings = json.loads(STRINGS.read_text(encoding="utf-8"))
    problems = [f"_attr_name left at {w}" for w in remaining_attr_names()]
    want = expected_block(strings.get("entity", {}))
    if strings.get("entity") != want:
        problems.append(
            "strings.json's entity block is not what the code names (run --write)"
        )
    if EN.read_text(encoding="utf-8") != STRINGS.read_text(encoding="utf-8"):
        problems.append("translations/en.json differs from strings.json")
    return problems


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    problems = check()
    print("\n".join(problems) or "entity translations: in step")
    sys.exit(1 if problems else 0)
