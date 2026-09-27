"""The lowest layer -- illegally imports the high layer. This is the
`solver_inputs -> solver_writer` late-import shape from docs/architecture/
tech-debt-plan.md section 1, reproduced at the smallest possible scale."""

from . import high

DOUBLED = high.VALUE * 2
