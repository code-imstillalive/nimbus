"""Synthetic fixture package for tests/test_gates_import_linter.py.

Same shape as the layer_ok fixture beside this one, except `low` imports
`high` -- the wrong direction, an unrecorded violation with no
`ignore_imports` exception for it. The contract must fail on this.
"""
