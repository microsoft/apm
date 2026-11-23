"""Scripted HTTP transport for in-process tests of ``requests``-based owners.

``ScriptedTransport`` replaces only ``requests.adapters.HTTPAdapter.send``, the
socket boundary under every ``requests.Session``. Everything above it -- the
owner's session, headers, credential scoping, redirect handling, streaming
and size caps -- runs for real. Each request is answered by the first route
whose predicate accepts it; every prepared request is recorded for assertions.
"""

from __future__ import annotations

import http.client
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


class _RawStream(io.BytesIO):
    """Body stream carrying the parsed header message, as urllib3 responses do.

    ``requests`` extracts ``Set-Cookie`` into the session jar only through
    ``raw._original_response.msg``; without it cookies would never persist.
    """

    def __init__(self, data: bytes, headers: dict[str, str]) -> None:
        super().__init__(data)
        block = "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
        self._original_response = type(
            "_Original", (), {"msg": http.client.parse_headers(io.BytesIO(block.encode()))}
        )()


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
    response.raw = _RawStream(data, headers or {})
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
    send_kwargs: list[dict] = field(default_factory=list)
    responses: list[requests.Response] = field(default_factory=list)

    def on(self, predicate: Callable[[requests.PreparedRequest], bool], respond: Responder):
        self.routes.append((predicate, respond))
        return self

    def sequence(self, *responders: Responder):
        """Answer successive requests with *responders* in order."""
        pending = list(responders)

        def _next(request: requests.PreparedRequest) -> requests.Response:
            if not pending:
                raise AssertionError(f"no scripted response left for {request.url}")
            return pending.pop(0)(request)

        return self.always(_next)

    def on_host(self, host: str, respond: Responder):
        return self.on(lambda request: urlsplit(request.url or "").hostname == host, respond)

    def always(self, respond: Responder):
        return self.on(lambda _request: True, respond)

    def _send(self, _adapter, request, **kwargs):
        self.requests.append(request)
        self.send_kwargs.append(kwargs)
        for predicate, respond in self.routes:
            if predicate(request):
                response = respond(request)
                self.responses.append(response)
                return response
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    @contextmanager
    def installed(self) -> Iterator[ScriptedTransport]:
        with patch("requests.adapters.HTTPAdapter.send", autospec=True, side_effect=self._send):
            yield self
