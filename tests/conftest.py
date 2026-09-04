"""Root pytest conftest -- currently just one thing: a guarded warning
filter that used to live in pyproject.toml's own [tool.pytest.ini_options]
filterwarnings list.

nimbus issue #358 (Mark Purcell): pytest resolves a dotted-path
`ignore::pkg.mod.SomeWarning` ini entry during its own config bootstrap,
which means IMPORTING `pkg.mod` at that point -- before conftest.py hooks,
before any test collection. `aiohttp` is never a base [project.dependencies]
requirement (it's only ever pulled in transitively via the [dev] extra's
homeassistant/pytest-homeassistant-custom-component deps), so a fresh
clone doing a plain `pip install -e .` (no [dev]) aborted every single
pytest invocation with "Failed to import filter module 'aiohttp'" before
a single test could run.

Doing the same ignore programmatically in pytest_configure(), guarded by
a plain try/except ImportError, degrades gracefully instead: aiohttp
installed (the real CI/dev environment) -> filter applies exactly as
before; aiohttp missing (a fresh clone with only base deps) -> a no-op,
tests can at least start collecting.
"""

from __future__ import annotations

import warnings


def pytest_configure(config) -> None:
    # Root cause of the #92 "0 live entity instances" / "Setup failed for
    # dependencies: ['http']" CI failure (2026-08-23): HA core's own stock
    # http component does `self.app["hass"] = self.hass` (a plain string
    # key) at homeassistant/components/http/server.py:290 -- unrelated to
    # anything in this repo's own diff. aiohttp (whatever version pip
    # resolves against the pinned homeassistant release; neither package
    # hard-pins the other) treats that as soft-deprecated and calls
    # warnings.warn(NotAppKeyWarning). pyproject.toml's own `"error"`
    # filter entry promotes every otherwise-unmatched warning to a hard
    # exception, which would crash http's own setup and cascade into
    # nimbus_load failing (http is a declared manifest dependency, needed
    # for frontend.py's static-path registration) -- this is HA core's
    # own internal housekeeping warning, not a signal about anything this
    # project owns.
    try:
        from aiohttp.web_exceptions import NotAppKeyWarning
    except ImportError:
        return
    warnings.filterwarnings("ignore", category=NotAppKeyWarning)
