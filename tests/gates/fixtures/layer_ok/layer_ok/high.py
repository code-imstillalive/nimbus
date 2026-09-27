"""The highest layer -- allowed to import the low layer, and does."""

from . import low

DOUBLED = low.VALUE * 2
