"""Synthetic fixture package for tests/test_gates_import_linter.py.

Same violation shape as layer_violation beside this one (`low` imports
`high`), except this fixture's own `.importlinter` records that one edge
under `ignore_imports` -- the exact mechanism pyproject.toml's real
`nimbus-layers` contract uses to record the tech-debt plan's seven known
`_solver_writer()` late imports. The contract must KEEP despite the real
structural violation, and must still catch a SECOND, unrecorded violation
introduced beside it (see test_a_new_violation_still_fails_even_with_an_
existing_exception in tests/test_gates_import_linter.py).
"""
