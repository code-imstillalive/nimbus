"""Standalone runner for the reference-household benchmark (nimbus issue
#273, item #3) -- `python tests/run_reference_benchmark.py` from the
repo root. Prints a human-readable summary plus a single JSON line, for
comparing across Nimbus releases. See solver/reference_benchmark.py's
own module docstring for the full methodology and why this is a number
to watch/record, not a CI gate.

Not a test itself (no assertions) -- lives alongside the tests because
it reuses _solver_path.py's already-proven sys.path setup rather than
duplicating that logic, same reasoning as every real test module here.
"""

import json
from pathlib import Path

import _solver_path  # noqa: F401
from solver.reference_benchmark import run_reference_benchmark


def main() -> None:
    result = run_reference_benchmark()
    r = result.regret

    version = "unknown"
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "nimbus_load"
        / "manifest.json"
    )
    try:
        version = json.loads(manifest_path.read_text(encoding="utf-8"))["version"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    print(
        f"Nimbus reference-household benchmark "
        f"(scenario v{result.scenario_version}, nimbus v{version})"
    )
    print(f"  J_star (oracle, perfect foresight):        ${r.j_star:.4f}")
    print(f"  J_forecast (Nimbus-style forecast input):  ${r.j_forecast:.4f}")
    print(f"  J_persistence (naive same-hour-lag):       ${r.j_persistence:.4f}")
    print(
        f"  forecast_regret_dollars (J_forecast-J*):    ${r.forecast_regret_dollars:.4f}"
    )
    print(
        f"  persistence_regret_dollars (J_pers-J*):     ${r.persistence_regret_dollars:.4f}"
    )
    print(
        f"  nimbus_value_add_dollars (J_pers-J_fcst):   ${r.nimbus_value_add_dollars:.4f}"
    )
    print()
    print(
        json.dumps(
            {
                "nimbus_version": version,
                "scenario_version": result.scenario_version,
                "j_star": r.j_star,
                "j_forecast": r.j_forecast,
                "j_persistence": r.j_persistence,
                "nimbus_value_add_dollars": r.nimbus_value_add_dollars,
            }
        )
    )


if __name__ == "__main__":
    main()
