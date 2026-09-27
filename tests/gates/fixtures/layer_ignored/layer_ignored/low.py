"""The lowest layer -- illegally imports the high layer, the one edge
this fixture's own `.importlinter` records under `ignore_imports`."""

from . import high

DOUBLED = high.VALUE * 2
