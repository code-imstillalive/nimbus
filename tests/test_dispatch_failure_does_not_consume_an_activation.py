"""nimbus issues #875 / #534: the two things the dispatch path currently
gets right, pinned structurally so a refactor cannot quietly undo them.

Found while auditing untested `except` guards. The ON/OFF dispatch
branches in `apply_commanded_state_guard()` are the highest-consequence
pair in that audit -- they sit directly on the path that switches real
hardware -- and neither branch had any test.

**These tests deliberately assert code shape, not behaviour.** That is
unusual here and needs justifying. The behaviour would need a real event
loop, a real `Store`, and a real `_NATIVE_HASS` with a running loop,
because the function dispatches through
`asyncio.run_coroutine_threadsafe(...)`. The invariants below are
genuinely structural anyway -- they are about *what is reachable when an
await raises*, which is a property of where a call sits relative to a
`try`, and a behavioural test would be asserting that arrangement
indirectly through a great deal of scaffolding.

What is pinned:

1. **A failed ON dispatch must not consume one of the day's
   activations.** `record_activation()` sits inside the same `try` as the
   `await dispatch_commanded_state(..., True, ...)`, after it, so it
   cannot run if the dispatch raised. #534 caps performance activations
   at a real device-side limit, so spending one on a command that never
   went out would burn a scarce, physically-meaningful resource on
   nothing -- and the cap then blocks the retry.

2. **Releasing a load must never consume an activation.** The OFF branch
   has no `record_activation()` at all. The docstring is explicit:
   *"An OFF transition is never capped -- #534's own cap is specifically
   on 'performance activations', not on releasing a load."*

3. **Neither dispatch failure may be swallowed silently.** Same lesson as
   the six report guards (v0.94.329): every one of those `except`
   branches was once a bare `pass`, and that hid a real incident in the
   log for days.

What is NOT asserted here, deliberately: that a failed dispatch should
not be persisted as a successful command. It currently is -- `new`
already carries `commanded_state=True` and is written regardless -- and
because dispatch is edge-triggered, nothing ever retries. That is a real
defect, reported on #875, and fixing it changes dispatch on live hot
water. Pinning the current behaviour would make the fix harder to land,
so this file stays silent on it.

One further observation, also left unasserted. The *whole-function*
handler logs at `_LOGGER.debug`, matching its own docstring ("Best-effort
and silent on any WHOLE-FUNCTION failure"). That is a deliberate,
separate decision from the two inner handlers, so the check below scopes
itself to the innermost `try` around each dispatch rather than sweeping
it up. Worth knowing, though, that a failure of the entire guard -- the
`future.result(timeout=10)` timing out, say -- is therefore invisible on
any install logging at WARNING, which is the normal case. That is the
same shape as the bare-`pass` lesson, one level up, and it is a judgement
call (a persistent failure would log every cycle) rather than something
to change from a test.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_SOLVER_WRITER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "nimbus_load"
    / "solver_writer.py"
)
_FUNC = "apply_commanded_state_guard"


def _function_node() -> ast.AST:
    tree = ast.parse(_SOLVER_WRITER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == _FUNC
        ):
            return node
    raise AssertionError(f"{_FUNC}() not found -- rename it and update this file")


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                out.append(n)
    return out


def _in_body(try_node: ast.Try, call: ast.Call) -> bool:
    """True when `call` is in the try's BODY rather than its handlers."""
    return any(call in set(ast.walk(stmt)) for stmt in try_node.body)


def _dispatch_on_calls(node: ast.AST) -> list[ast.Call]:
    """`dispatch_commanded_state(..., True, ...)` -- the ON dispatches."""
    out = []
    for call in _calls_named(node, "dispatch_commanded_state"):
        for arg in call.args:
            if isinstance(arg, ast.Constant) and arg.value is True:
                out.append(call)
                break
    return out


class TestAFailedOnDispatchCannotConsumeAnActivation(unittest.TestCase):
    def setUp(self):
        self.fn = _function_node()
        self.tries = [t for t in ast.walk(self.fn) if isinstance(t, ast.Try)]

    def test_the_function_still_records_activations_at_all(self):
        """Guards the assertion below from passing because the call was
        renamed or removed rather than because it is placed correctly."""
        self.assertTrue(
            _calls_named(self.fn, "record_activation"),
            f"{_FUNC}() no longer calls record_activation() anywhere -- if that "
            "is deliberate, #534's activation cap needs re-checking, not this "
            "test deleting",
        )

    def test_every_record_activation_is_guarded_by_the_dispatch_it_follows(self):
        for call in _calls_named(self.fn, "record_activation"):
            enclosing = [
                t for t in self.tries if _in_body(t, call) and _dispatch_on_calls(t)
            ]
            with self.subTest(line=call.lineno):
                self.assertTrue(
                    enclosing,
                    f"record_activation() at line {call.lineno} is not inside a "
                    "try whose body dispatches ON. If the dispatch raises, this "
                    "would still burn one of #534's capped activations on a "
                    "command that never went out -- and the cap then blocks the "
                    "retry.",
                )

    def test_it_runs_after_the_dispatch_not_before(self):
        """Inside the same `try` is necessary but not sufficient: placed
        before the await, it would run even when the dispatch fails."""
        for call in _calls_named(self.fn, "record_activation"):
            for t in self.tries:
                if not (_in_body(t, call) and _dispatch_on_calls(t)):
                    continue
                first_dispatch = min(c.lineno for c in _dispatch_on_calls(t))
                with self.subTest(line=call.lineno):
                    self.assertGreater(
                        call.lineno,
                        first_dispatch,
                        "record_activation() runs before the ON dispatch it is "
                        "meant to be guarded by, so a failed dispatch would "
                        "still consume an activation",
                    )


class TestReleasingALoadIsNeverCapped(unittest.TestCase):
    """The docstring's own words: "An OFF transition is never capped --
    #534's own cap is specifically on 'performance activations', not on
    releasing a load." """

    def test_the_off_dispatch_try_records_no_activation(self):
        fn = _function_node()
        off_tries = [
            t
            for t in ast.walk(fn)
            if isinstance(t, ast.Try)
            and _calls_named(t, "dispatch_commanded_state")
            and not _dispatch_on_calls(t)
        ]
        self.assertTrue(
            off_tries,
            "no OFF-only dispatch try found -- the ON/OFF branches may have "
            "been restructured; re-check this invariant by hand",
        )
        for t in off_tries:
            with self.subTest(line=t.lineno):
                self.assertEqual(
                    _calls_named(t, "record_activation"),
                    [],
                    "the OFF dispatch records an activation -- turning a load "
                    "OFF would then count against #534's cap on turning it ON",
                )


class TestNeitherDispatchFailureIsSwallowed(unittest.TestCase):
    """Same lesson as the six report guards: every one of those `except`
    branches was once a bare `pass`, and it hid a real incident in the
    log for days. These two sit on the path that switches real
    hardware."""

    def test_each_dispatch_handler_logs(self):
        fn = _function_node()
        all_tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)]
        # The INNERMOST try around each dispatch -- the one whose handler
        # actually catches that call. The whole-function try also contains
        # them transitively, and it logs at DEBUG on purpose (its own
        # docstring: "Best-effort and silent on any WHOLE-FUNCTION
        # failure"), which is a different decision from these.
        dispatch_tries = []
        for call in _calls_named(fn, "dispatch_commanded_state"):
            enclosing = [t for t in all_tries if _in_body(t, call)]
            if enclosing:
                dispatch_tries.append(max(enclosing, key=lambda t: t.lineno))
        self.assertTrue(dispatch_tries)
        for t in dispatch_tries:
            for handler in t.handlers:
                logged = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in {"warning", "error", "exception"}
                    for stmt in handler.body
                    for n in ast.walk(stmt)
                )
                with self.subTest(line=handler.lineno):
                    self.assertTrue(
                        logged,
                        f"the except handler at line {handler.lineno} swallows a "
                        "dispatch failure without logging it -- that is the "
                        "bare-`pass` behaviour that made a real incident "
                        "invisible for days",
                    )


if __name__ == "__main__":
    unittest.main()
