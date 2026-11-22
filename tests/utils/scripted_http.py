"""Scripted HTTP transport for in-process tests of ``requests``-based owners.

``ScriptedTransport`` replaces only ``requests.adapters.HTTPAdapter.send``, the
socket boundary under every ``requests.Session``. Everything above it -- the
owner's session, headers, credential scoping, redirect handling, streaming
and size caps -- runs for real. Each request is answered by the first route
whose predicate accepts it; every prepared request is recorded for assertions.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from requests.structures import CaseInsensitiveDict

Responder = Callable[[requests.PreparedRequest], requests.Response]


def make_response(
    request: requests.PreparedRequest,
    status: int = 200,
    *,
    body: bytes | str = b"",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Build a real streamed ``requests.Response`` for *request*."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response.raw = io.BytesIO(data)
    response.url = request.url or ""
    response.request = request
    response.encoding = "utf-8"
    response.reason = "scripted"
    return response


def json_body(payload: object, status: int = 200, **headers: str) -> Responder:
    """Answer with *payload* serialised as ``application/json``."""
    body = json.dumps(payload)

    def _respond(request: requests.PreparedRequest) -> requests.Response:
        return make_response(
            request,
            status,
            body=body,
            headers={"Content-Type": "application/json", **headers},
        )

    return _respond


def text_body(text: str, status: int = 200, content_type: str = "text/plain") -> Responder:
    """Answer with *text* under *content_type*."""

    def _respond(request: requests.PreparedRequest) -> requests.Response:
        return make_response(request, status, body=text, headers={"Content-Type": content_type})

    return _respond


def status(code: int, **headers: str) -> Responder:
    """Answer with an empty body and *code*."""

    def _respond(request: requests.PreparedRequest) -> requests.Response:
        return make_response(request, code, headers=dict(headers))

    return _respond


def raising(error: Exception) -> Responder:
    """Fail the send with *error* (for example ``requests.ConnectionError``)."""

    def _respond(_request: requests.PreparedRequest) -> requests.Response:
        raise error

    return _respond


@dataclass
class ScriptedTransport:
    """Route table and request log for one test."""

    routes: list[tuple[Callable[[requests.PreparedRequest], bool], Responder]] = field(
        default_factory=list
    )
    requests: list[requests.PreparedRequest] = field(default_factory=list)

    def on(self, predicate: Callable[[requests.PreparedRequest], bool], respond: Responder):
        self.routes.append((predicate, respond))
        return self

    def on_host(self, host: str, respond: Responder):
        return self.on(lambda request: urlsplit(request.url or "").hostname == host, respond)

    def always(self, respond: Responder):
        return self.on(lambda _request: True, respond)

    def _send(self, _adapter, request, **_kwargs):
        self.requests.append(request)
        for predicate, respond in self.routes:
            if predicate(request):
                return respond(request)
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    @contextmanager
    def installed(self) -> Iterator[ScriptedTransport]:
        with patch("requests.adapters.HTTPAdapter.send", autospec=True, side_effect=self._send):
            yield self
