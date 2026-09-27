# Spec NNN: <name>

Status: proposed | approved | implemented (PR #) | released (vX.Y.Z)
Plan: docs/architecture/tech-debt-plan.md; phase: #NNNN

## Responsibility

One paragraph: the single responsibility this spec moves, where it lives today
(file and line ranges at a named commit), and why it changes independently of
what it sits beside.

## Measured cost

Paste the output of both, at the commit named above:

- `python tests/analyse_module_dependencies.py` for this phase: lines in scope,
  module-level dependencies per function.
- `python tests/analyse_module_dependencies.py --callers`: production callers,
  test references and monkeypatch sites per name.

## Current behaviour this must preserve

Every observable output the moved code affects, with the code location that
produces it and what pins it. Observable means: states and attributes posted,
services called, entities read, log lines tests assert on, state file formats
(`plan_state.json` and the other `NIMBUS_SOLVER_*_PATH` files).

| Behaviour | Produced at | Pinned by |
|---|---|---|
| | `module.py:line` | golden scenario / `tests/test_x.py::test_y` |

Name the golden scenarios that reach the moved code, with the per-function
coverage they give it. If none reach it, the spec adds one first.

## Interfaces

New modules and protocols as typed signatures, with the layer each belongs
to. No bodies. Dependencies arrive as parameters or protocols, never through
`_solver_writer()`.

```python
class HistoryReader(Protocol):
    def history(self, entity_id: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]: ...
```

## Façade decision

For each moved name, from the `--callers` table: re-exported (a production
caller or monkeypatch site resolves it at call time) or not (none does). List
every patch path retargeted.

## Invariants

Statements that must hold after the change and that a test can check.

## Migration

Numbered, each step leaving the suite green:

1. Add the new unit beside the old code, with contract tests.
2. Point callers at it one at a time.
3. Delete the old code; keep re-exports only where the façade decision says so.

## Non-goals

What this spec does not change, including defects noticed on the way (file
them as issues and link them here).

## Acceptance

The verifier checks each item and cites evidence.

- [ ] Golden master identical for every scenario (`tests/test_golden_master.py`).
- [ ] Full suite passes; no existing assertion edited; new tests listed here: ...
- [ ] No monkeypatch target became a no-op.
- [ ] No line executed before is unexecuted after.
- [ ] mypy count not higher (from N to M); zero in new modules.
- [ ] Import contracts: no new violation; moved code in its target layer.
- [ ] No new function over 60 lines; count over 60 not higher (from N to M).
- [ ] No `_solver_writer()` late import added.
- [ ] Spec-specific: ...

## Rollback

Which commit to revert, and whether any state file written by the new code
needs attention.
