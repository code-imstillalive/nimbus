"""No file the solver DELETES may live inside HA's backed-up config tree.

## The real failure this pins

Production, 04:45 AEST 2026-09-27. HA's automatic backup aborted after 24 seconds
having written ~20 MB:

    FileNotFoundError: [Errno 2] No such file or directory:
        '/config/nimbus_solver_writer.lock'
      securetar/__init__.py  _atomic_contents_add -> tar_file.add(...)
      tarfile.py             add -> gettarinfo -> os.lstat(name)

`securetar` enumerates `/config`, then `os.lstat()`s each entry it found. The
solver released its lock and **deleted** the file between those two steps, and a
single missing entry aborts the whole archive. Disk was not the cause: the node
reported 30% used and 613 GB free, and it held the VIP.

The lock was written and removed on **every solve cycle** -- roughly 1,440
collision windows a night against one backup walk -- so this was a recurring
coin-flip. And it fails quietly: HA raises no repair, the backup entity just
keeps its last successful timestamp, so the household found it five hours later
by noticing the file size.

## The invariant, stated as the property rather than the path

**Writing a file inside `/config` is safe; DELETING one is not.** An overwritten
file still exists when `lstat()` reaches it. So the test is not "is the lock in
/tmp" -- that would pass while a future third deleted file reintroduced the bug.
It is: *every path the native runtime puts under the config dir must be one the
solver never unlinks.*

That is why `TestNoDeletedFileLivesUnderConfig` reads `solver_writer.py`'s own
`os.remove()` call sites out of the AST and cross-references them against the
paths `set_default_env_vars()` points at `hass.config.path()`. A new deleted file
placed in /config fails here without anyone remembering this incident.

## What deliberately stays in /config

`nimbus_solver_last_plan.json` and `nimbus_solver_solar_delivery_ratio.json` are
plain overwrites that are never unlinked, and they are real state a restore
should bring back. Moving them would be a different bug.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime

_PKG = Path(solver_runtime.__file__).parent
_RUNTIME_SRC = Path(solver_runtime.__file__).read_text(encoding="utf-8")
_WRITER_SRC = (_PKG / "solver_writer.py").read_text(encoding="utf-8")

#: solver_writer.py plus every module #1298 has extracted out of it that can
#: hold an `os.remove()` of a persisted path. Deliberately explicit rather than
#: a glob: the HA platform modules are not the writer.
_WRITER_SRCS = [
    _WRITER_SRC,
    (_PKG / "solver" / "cycle_lock.py").read_text(encoding="utf-8"),
]

#: The env vars the native runtime resolves, and what each one holds.
_TRANSIENT = {
    "NIMBUS_SOLVER_LOCK_PATH",
    "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH",
}
_PERSISTENT = {
    "NIMBUS_SOLVER_PLAN_STATE_PATH",
    "NIMBUS_SOLVER_SOLAR_DELIVERY_RATIO_PATH",
}


def _setdefault_calls() -> dict[str, ast.AST]:
    """Map each env var `set_default_env_vars()` sets to its value expression."""
    tree = ast.parse(_RUNTIME_SRC)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "set_default_env_vars"
    )
    out: dict[str, ast.AST] = {}
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "setdefault"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out[node.args[0].value] = node.args[1] if len(node.args) > 1 else node
    return out


def _uses_hass_config_path(expr: ast.AST) -> bool:
    """Does this value expression route through `hass.config.path(...)`?

    That call is what puts a file inside the backed-up tree, so it is the thing
    worth detecting -- not the literal string "/config", which never appears.
    """
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "path":
            base = node.func.value  # type: ignore[attr-defined]
            if (
                isinstance(base, ast.Attribute)
                and base.attr == "config"
                and isinstance(base.value, ast.Name)
                and base.value.id == "hass"
            ):
                return True
    return False


def _os_remove_arg_names(tree: ast.Module) -> set[tuple[str, str | None]]:
    """`(name, enclosing function)` for every `os.remove(<Name>)` in one tree.

    The call graph, not a text search: this file's comments discuss removal
    constantly, and a prose match would flag files nothing unlinks.
    """
    enclosing: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                enclosing.setdefault(child, fn.name)

    found: set[tuple[str, str | None]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "remove"
            and isinstance(getattr(node.func, "value", None), ast.Name)
            and node.func.value.id == "os"  # type: ignore[attr-defined]
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            found.add((node.args[0].id, enclosing.get(node)))
    return found


def _params_of(tree: ast.Module, func_name: str) -> list[str]:
    for fn in ast.walk(tree):
        if (
            isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and fn.name == func_name
        ):
            return [a.arg for a in fn.args.args]
    return []


def _args_passed_to(trees: list[ast.Module], func_name: str, index: int) -> set[str]:
    """Every `ast.Name` passed positionally at `index` to `func_name(...)`.

    Matches both `release_lock(X)` and `cycle_lock.release_lock(X)`, since the
    wrapper reaches the extracted module by attribute.
    """
    names: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) <= index:
                continue
            f = node.func
            called = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if called == func_name and isinstance(node.args[index], ast.Name):
                names.add(node.args[index].id)
    return names


def _removed_path_names() -> set[str]:
    """Module-level constants whose file `os.remove()` unlinks at runtime.

    Resolved **through one wrapper**, because nimbus #1306 (spec 007, Phase 7a)
    moved the overlap guard into `solver/cycle_lock.py` with the path as a
    parameter: the unlink now reads `os.remove(lock_path)` there, and it is
    `solver_writer.release_lock` that supplies `LOCK_PATH`. Without following
    that hop this guard would have silently stopped tracking `LOCK_PATH` --
    still passing, checking one file instead of two.
    """
    trees = [ast.parse(src) for src in _WRITER_SRCS]
    removed: set[str] = set()
    for tree in trees:
        for name, func in _os_remove_arg_names(tree):
            params = _params_of(tree, func) if func else []
            if name in params:
                # A parameter, not a constant: resolve it to whatever the
                # caller passes at that position.
                removed |= _args_passed_to(trees, func, params.index(name))
            else:
                removed.add(name)
    return removed


#: Which module-level constant in solver_writer.py each env var feeds.
_ENV_TO_CONST = {
    "NIMBUS_SOLVER_LOCK_PATH": "LOCK_PATH",
    "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH": "LOAD_FORECAST_ERROR_NOTIFIED_PATH",
    "NIMBUS_SOLVER_PLAN_STATE_PATH": "PLAN_STATE_PATH",
    "NIMBUS_SOLVER_SOLAR_DELIVERY_RATIO_PATH": "SOLAR_DELIVERY_RATIO_PATH",
}


class TestNoDeletedFileLivesUnderConfig(unittest.TestCase):
    """The invariant, derived rather than hardcoded.

    Cross-references the files the solver `os.remove()`s against the paths the
    native runtime places under `hass.config.path()`. The intersection must be
    empty -- and it is empty for a reason a future contributor can rediscover
    from the failure message alone.
    """

    def test_the_intersection_is_empty(self):
        setdefaults = _setdefault_calls()
        removed_consts = _removed_path_names()
        offenders = []
        for env, expr in setdefaults.items():
            const = _ENV_TO_CONST.get(env)
            if const is None:
                continue
            if const in removed_consts and _uses_hass_config_path(expr):
                offenders.append(f"{env} -> {const}")
        self.assertEqual(
            offenders,
            [],
            "these are DELETED by the solver and placed inside HA's backed-up "
            "config tree, which aborts an automatic backup with FileNotFoundError "
            "when securetar lstat()s an entry that vanished between listing and "
            "stat (nimbus #1324, real production failure 2026-09-27): "
            + ", ".join(offenders),
        )

    def test_the_two_deleted_files_are_actually_detected_as_deleted(self):
        """Guard against the guard passing vacuously.

        If `os.remove()` were ever refactored behind a helper, the set above
        would go empty and the real test would pass while checking nothing.
        """
        removed = _removed_path_names()
        self.assertIn("LOCK_PATH", removed)
        self.assertIn("LOAD_FORECAST_ERROR_NOTIFIED_PATH", removed)

    def test_the_persistent_files_still_live_under_config(self):
        """The other half of the invariant: real state belongs in the backup.

        Moving these out would be a different bug -- a restore would silently
        lose the last published plan and the solar delivery ratio.
        """
        setdefaults = _setdefault_calls()
        for env in _PERSISTENT:
            with self.subTest(env=env):
                self.assertIn(env, setdefaults)
                self.assertTrue(
                    _uses_hass_config_path(setdefaults[env]),
                    f"{env} holds real state a restore must bring back, so it "
                    f"belongs under hass.config.path()",
                )
                self.assertNotIn(_ENV_TO_CONST[env], _removed_path_names())


class TestTheTransientFilesResolveSomewhereSane(unittest.TestCase):
    """Behavioural: run the real resolver and look at what it produced."""

    class _Config:
        def __init__(self, config_dir: str) -> None:
            self.config_dir = config_dir
            self.time_zone = "Australia/Brisbane"

        def path(self, *parts: str) -> str:
            import os

            return os.path.join(self.config_dir, *parts)

    class _Hass:
        def __init__(self, config_dir: str) -> None:
            self.config = TestTheTransientFilesResolveSomewhereSane._Config(config_dir)

    def _resolve(self, config_dir: str) -> dict[str, str]:
        import os

        saved = {k: os.environ.get(k) for k in _TRANSIENT | _PERSISTENT}
        for k in saved:
            os.environ.pop(k, None)
        try:
            solver_runtime.set_default_env_vars(self._Hass(config_dir))
            return {k: os.environ[k] for k in _TRANSIENT | _PERSISTENT}
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_the_deleted_files_land_outside_the_config_dir(self):
        resolved = self._resolve("/config")
        for env in _TRANSIENT:
            with self.subTest(env=env):
                self.assertNotIn(
                    "/config",
                    resolved[env].replace("\\", "/"),
                    f"{env} must not resolve inside the backed-up tree",
                )

    def test_two_installs_on_one_host_do_not_share_a_lock(self):
        """/config is per-install; a temp dir is shared. A lock whose whole job
        is keeping two processes apart must not become the thing that collides
        them -- that would be a worse bug than the one being fixed."""
        a = self._resolve("/config")
        b = self._resolve("/other/config")
        self.assertNotEqual(
            a["NIMBUS_SOLVER_LOCK_PATH"],
            b["NIMBUS_SOLVER_LOCK_PATH"],
            "two HA instances on one host would share a lock file",
        )

    def test_the_same_install_resolves_the_same_path_every_time(self):
        """The guard is worthless if the path moves between cycles."""
        self.assertEqual(
            self._resolve("/config")["NIMBUS_SOLVER_LOCK_PATH"],
            self._resolve("/config")["NIMBUS_SOLVER_LOCK_PATH"],
        )

    def test_the_persistent_files_still_resolve_into_the_config_dir(self):
        resolved = self._resolve("/config")
        for env in _PERSISTENT:
            with self.subTest(env=env):
                self.assertIn("/config", resolved[env].replace("\\", "/"))

    def test_an_explicit_env_override_still_wins(self):
        """`setdefault`, not assignment: the standalone/cron deployment sets
        these itself and must keep its own /opt paths."""
        import os

        sentinel = "/opt/nimbus_solver_forecast_writer.lock"
        saved = os.environ.get("NIMBUS_SOLVER_LOCK_PATH")
        os.environ["NIMBUS_SOLVER_LOCK_PATH"] = sentinel
        try:
            solver_runtime.set_default_env_vars(self._Hass("/config"))
            self.assertEqual(os.environ["NIMBUS_SOLVER_LOCK_PATH"], sentinel)
        finally:
            if saved is None:
                os.environ.pop("NIMBUS_SOLVER_LOCK_PATH", None)
            else:
                os.environ["NIMBUS_SOLVER_LOCK_PATH"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
