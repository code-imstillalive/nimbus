"""Synthetic fixture for tests/test_gates_mypy_ratchet.py's "good case":
a tiny module mypy reports zero findings against, on any reasonably
current mypy release. Deliberately trivial -- this fixture exists to
prove the ratchet script correctly reports and accepts a clean run, not
to exercise mypy's own type-checking depth."""

from __future__ import annotations


def add(a: int, b: int) -> int:
    return a + b


TOTAL: int = add(1, 2)
