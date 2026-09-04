"""Regression test for nimbus issue #363 (Mark Purcell, codebase review),
findings 1 and 2: `solver_writer.py` had 23 operational `print(...,
file=sys.stderr)` sites invisible to HA's own log, and 8+ bare `except
Exception: return`/`pass` sites with zero breadcrumb at all -- the same
silent-skip pattern #313/#314 already fixed elsewhere in this file.

Both fixed: every operational print() (source unavailable, SoC outside
range, fixed-export violation clamp, plan-state save failure, safe_num
fallback, solar source dropped, and more) is now `_LOGGER.warning(...)`;
every bare `except Exception` now logs at DEBUG or WARNING before
degrading, preserving each site's own deliberate "never break the real
solve" contract while making a real failure diagnosable.

This test uses the real AST (not a fragile source-text regex) to
structurally verify both properties hold, so a future new print()/silent
except added anywhere in this large (7000+ line) file is caught here
rather than silently reintroducing the exact pattern this issue fixed.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_SOLVER_WRITER_PY = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)


def _load_tree() -> tuple[ast.Module, list[ast.stmt]]:
    src = _SOLVER_WRITER_PY.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(_SOLVER_WRITER_PY))
    return tree, tree.body


def _is_main_guard(node: ast.stmt) -> bool:
    """True for the top-level `if __name__ == "__main__":` block."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def _logs_something(node: ast.AST) -> bool:
    """True if `_LOGGER.<anything>(...)` is called anywhere inside node."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            value = child.func.value
            if isinstance(value, ast.Name) and value.id == "_LOGGER":
                return True
    return False


class TestNoOperationalPrintOutsideMainGuard(unittest.TestCase):
    def test_print_call_sites_outside_main_guard_are_the_known_deliberate_ones(self):
        tree, top_level = _load_tree()
        main_guard_nodes = {id(n) for n in top_level if _is_main_guard(n)}

        def _in_main_guard(node: ast.AST) -> bool:
            # Walk every top-level main-guard subtree looking for this
            # exact node object.
            return any(
                any(child is node for child in ast.walk(guard))
                for guard in top_level
                if id(guard) in main_guard_nodes
            )

        print_calls_outside_guard = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
                and not _in_main_guard(node)
            ):
                print_calls_outside_guard.append(node.lineno)

        # Exactly one deliberate, known survivor: the per-cycle status
        # summary inside main() itself, kept as a real print() (not
        # _LOGGER.info()) specifically so it stays visible when tailing
        # the standalone/cron deployment's own stdout -- _LOGGER.info()
        # would be silent by default there (Python's own `logging.
        # lastResort` handler only surfaces WARNING and above with zero
        # configuration, unlike WARNING-level messages, which is why
        # every OTHER print() in this file became _LOGGER.warning(), not
        # _LOGGER.info()).
        self.assertEqual(
            len(print_calls_outside_guard),
            1,
            f"expected exactly 1 deliberate print() outside the __main__ "
            f"guard (the per-cycle status summary), found "
            f"{len(print_calls_outside_guard)} at lines "
            f"{print_calls_outside_guard} -- a new print() here is "
            "invisible to HA's own log in native mode; use _LOGGER "
            "instead unless this is a deliberate, reviewed exception",
        )


class TestEveryBareExceptExceptionLogsSomething(unittest.TestCase):
    def test_every_except_exception_handler_calls_logger_or_reraises(self):
        tree, _top_level = _load_tree()
        offending: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # Only the broad `except Exception:` / `except Exception as e:`
            # shape -- a narrow, specific except (KeyError, ValueError,
            # etc.) is a different, already-precise contract this issue
            # never flagged.
            if not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                continue
            handler_logs = any(_logs_something(stmt) for stmt in node.body)
            handler_reraises = any(isinstance(stmt, ast.Raise) for stmt in node.body)
            if not handler_logs and not handler_reraises:
                offending.append(node.lineno)
        self.assertEqual(
            offending,
            [],
            f"found `except Exception` handler(s) with zero _LOGGER call "
            f"and no re-raise at line(s) {offending} -- a silently "
            "swallowed failure here is exactly the pattern nimbus issue "
            "#363 fixed; add a _LOGGER.debug/warning(..., exc_info=True) "
            "breadcrumb before degrading",
        )
