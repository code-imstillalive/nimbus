"""In-memory stand-in for Home Assistant's REST API, for the golden master.

Spec 000 Part A (docs/specs/000-golden-master-and-gates.md). The golden
master drives the real ``solver_writer.main()`` in standalone (REST) mode,
so the only thing faked is the other end of the eight REST endpoints that
``solver_writer`` calls through ``urllib.request.urlopen``:

    GET  /api/states/<entity_id>
    POST /api/states/<entity_id>
    POST /api/services/<domain>/<service>
    POST /api/services/<domain>/<service>?return_response
    GET  /api/history/period/<start>Z?filter_entity_id=...&end_time=...

Everything the module does with a response (parsing, fallbacks, error
handling) runs for real. Every request is recorded, in order, so the
snapshot pins what was read as well as what was written.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

BASE = "http://golden.invalid:8123"


@dataclass
class HistoryPoint:
    """One recorded state, as HA's ``minimal_response`` history returns it."""

    last_changed: str  # ISO 8601 with offset
    state: str
    attributes: Mapping[str, Any] | None = None


@dataclass
class FakeHA:
    """The recorded state of one fake Home Assistant instance."""

    states: dict[str, dict[str, Any]]
    history: Mapping[str, Sequence[HistoryPoint]] = field(default_factory=dict)
    service_responses: Mapping[str, Any] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    posted: list[dict[str, Any]] = field(default_factory=list)
    service_calls: list[dict[str, Any]] = field(default_factory=list)

    # -- urlopen replacement -------------------------------------------------

    def urlopen(self, req, timeout: float | None = None, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if not url.startswith(BASE):
            raise AssertionError(f"golden master: request left the fake HA: {url}")
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        query = parsed.query
        body = None
        if getattr(req, "data", None):
            body = json.loads(req.data.decode("utf-8"))
        self.requests.append({"method": method, "path": path, "query": query})

        if path.startswith("/api/states/"):
            entity_id = path[len("/api/states/") :]
            if method == "GET":
                if entity_id not in self.states:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                return _Response({"entity_id": entity_id, **self.states[entity_id]})
            self.posted.append({"entity_id": entity_id, **(body or {})})
            # A later GET of an entity the module itself published sees
            # what it wrote, as it would against a real instance.
            self.states[entity_id] = {
                "state": (body or {}).get("state"),
                "attributes": (body or {}).get("attributes") or {},
            }
            return _Response({})

        if path.startswith("/api/services/"):
            domain, service = path[len("/api/services/") :].split("/", 1)
            self.service_calls.append(
                {
                    "domain": domain,
                    "service": service,
                    "return_response": "return_response" in query,
                    "data": body,
                }
            )
            if "return_response" in query:
                key = f"{domain}.{service}"
                if key not in self.service_responses:
                    raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
                return _Response({"service_response": self.service_responses[key]})
            return _Response([])

        if path.startswith("/api/history/period/"):
            params = urllib.parse.parse_qs(query, keep_blank_values=True)
            ids = params.get("filter_entity_id", [""])[0].split(",")
            start = path[len("/api/history/period/") :]
            end = params.get("end_time", [None])[0]
            out = []
            for entity_id in ids:
                pts = [
                    p
                    for p in self.history.get(entity_id, ())
                    if _in_window(p.last_changed, start, end)
                ]
                if not pts:
                    out.append([])
                    continue
                rows = []
                for i, p in enumerate(pts):
                    row: dict[str, Any] = {
                        "state": p.state,
                        "last_changed": p.last_changed,
                    }
                    if i == 0 or p.attributes is not None:
                        row["entity_id"] = entity_id
                        row["attributes"] = dict(p.attributes or {})
                    rows.append(row)
                out.append(rows)
            return _Response(out)

        raise AssertionError(f"golden master: unhandled HA endpoint {method} {url}")


def _parse_utc(stamp: str):
    from datetime import UTC, datetime

    s = stamp.rstrip("Z")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _in_window(stamp: str, start: str, end: str | None) -> bool:
    t = _parse_utc(stamp)
    if t < _parse_utc(start):
        return False
    return end is None or t < _parse_utc(end)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self, *args) -> bytes:
        return self._buf.read(*args)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        return None


Installer = Callable[[FakeHA], None]
