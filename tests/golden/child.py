"""Child-process entry point for one golden-master scenario.

    python -m golden.child <scenario> <record.json> [cycles]

Run by ``harness.run_isolated`` with the environment it builds; not meant
to be run by hand except when debugging a scenario. Imports numpy (through
``solver_writer``) only after the environment pins are in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

# pytest's pythonpath setting, applied the way pytest applies it: after
# the interpreter and the stdlib modules above are loaded (harness.py).
sys.path.insert(0, os.environ["GOLDEN_PKG"])


def _file_record(raw: bytes) -> object:
    """A state file as the record holds it: parsed JSON, else text, else
    (for a file that is not UTF-8) its size and SHA-256, so a binary
    artifact is still compared rather than crashing the scenario."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"binary_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(argv: list[str]) -> int:
    name, out_path = argv[0], Path(argv[1])
    cycles = int(argv[2]) if len(argv) > 2 else 1

    from freezegun import freeze_time
    from golden import scenarios

    scenario = scenarios.get(name)
    fake = scenario.build()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    log_records: list[dict[str, str]] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                log_records.append(
                    {"level": record.levelname, "message": record.getMessage()}
                )

    logging.getLogger().addHandler(_Capture())

    workdir = Path(os.environ["GOLDEN_WORKDIR"])

    cycle_records = []
    native = None
    with patch.object(urllib.request, "urlopen", fake.urlopen):
        import solver_writer

        # nimbus issue #1335: native mode. The fake homeassistant.* modules
        # have to be in sys.modules before main() reaches the lazy imports
        # inside its native branches, and set_native_hass() before the first
        # cycle. Both are confined to this child process -- see
        # fake_native's own docstring for why neither may happen in the
        # pytest process.
        if scenario.native is not None:
            from golden import fake_native

            native = scenario.native(fake)
            fake_native.install_native_modules(native, workdir)
            solver_writer.set_native_hass(native)
            fake_native.register_managed_entity_handlers(solver_writer, native)

        for i in range(cycles):
            instant = scenario.instant_for_cycle(i)
            start_posted = len(fake.posted)
            start_calls = len(fake.service_calls)
            start_reqs = len(fake.requests)
            start_logs = len(log_records)
            with freeze_time(instant, tick=False):
                solver_writer.main()
            cycle_records.append(
                {
                    "instant": instant,
                    "posted": fake.posted[start_posted:],
                    "service_calls": fake.service_calls[start_calls:],
                    "requests": fake.requests[start_reqs:],
                    "warnings": log_records[start_logs:],
                }
            )

    if native is not None:
        # Before the state files are read back: the guard's own writes go
        # through this loop, and an event loop left running holds real OS
        # sockets open.
        native.close()

    files = {}
    for path in sorted(workdir.iterdir()):
        if path.name == out_path.name or not path.is_file():
            continue
        files[path.name] = _file_record(path.read_bytes())

    record = {
        "scenario": name,
        "python": list(sys.version_info[:3]),
        "cycles": cycle_records,
        "files": files,
    }
    out_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
