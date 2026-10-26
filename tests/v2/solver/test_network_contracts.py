"""Layer 1 of the rebuilt suite (#1837): two `network.py` contracts the 21 Oct
classification of the surviving mutants found neither test set checking
(seed 1837; the other three survivors are equivalent mutants, recorded on
#1837).

- The stochastic plan solves "the same inputs `network.build_plan(**
  build_kwargs)` would solve deterministically" (stochastic_dispatch.py),
  but calls `_build_plan_once` directly: an argument the caller leaves out
  must mean the same in both, so every default they share is equal.
- An adequacy load's reported profit is what its energy was worth less what
  it cost at the plan's own marginal price (#482), in kWh, so a period's
  length counts.
"""

from __future__ import annotations

import inspect

from solver import network


def test_build_plan_and_the_stochastic_entry_share_every_default():
    once = inspect.signature(network._build_plan_once).parameters
    public = inspect.signature(network.build_plan).parameters
    shared = [
        name
        for name, p in public.items()
        if name in once and p.default is not inspect.Parameter.empty
    ]
    assert "risk_aversion" in shared  # the check is not vacuous
    differ = {
        name: (public[name].default, once[name].default)
        for name in shared
        if public[name].default != once[name].default
    }
    assert differ == {}
