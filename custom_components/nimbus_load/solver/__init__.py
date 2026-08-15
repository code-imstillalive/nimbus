"""Nimbus Solver -- draft, observation-only. See README.md in this
directory before using anything here. Deliberately NOT imported by this
integration's own __init__.py/coordinator.py/config_flow.py -- nothing
in this subpackage runs, registers an entity, or writes anywhere unless
a caller explicitly imports it, which nothing in the live integration
currently does.
"""

from __future__ import annotations
