"""Synthetic fixture for tests/test_gates_mypy_ratchet.py's "bad case":
a tiny module with unambiguous, real type errors -- an int annotation
assigned a str, and a function call with an argument of the wrong type --
that any reasonably current mypy release reports as findings regardless
of Python-minor-version-driven typeshed differences (unlike, say, a
`--strict` Optional-narrowing edge case). Deliberately not subtle: this
fixture exists to prove the ratchet script correctly counts and rejects a
run whose count exceeds its baseline, not to exercise mypy's own
type-checking depth."""

from __future__ import annotations


def add(a: int, b: int) -> int:
    return a + b


WRONG_TYPE: int = "not an int"  # error 1: str is not int
BAD_CALL = add("also not an int", 2)  # error 2: str argument to an int parameter
