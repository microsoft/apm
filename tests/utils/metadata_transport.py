"""Drive urllib-shaped metadata fakes through the builder's HTTP owner seam.

Marketplace metadata enrichment sends every request through
``apm_cli.utils.http_credentials.origin_bounded_request``. Builder tests were
written against ``urllib.request.urlopen`` fakes (a callable taking a
``urllib.request.Request`` and returning a readable body, or raising
``urllib.error.HTTPError``/``URLError``). :func:`urlopen_adapter` turns such a
fake into an ``origin_bounded_request`` stand-in so the tests keep asserting
on the request URL and headers while production code uses ``requests``.
"""

from __future__ import annotations

import contextlib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import requests

BUILDER_HTTP_SEAM = "apm_cli.marketplace.builder.origin_bounded_request"


class _AdaptedResponse:
    """The slice of ``requests.Response`` the metadata reader consumes."""

    def __init__(self, status: int, body: bytes, url: str) -> None:
        self.status_code = status
        self.url = url
        self.headers: dict[str, str] = {}
        self._body = body

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        for start in range(0, len(self._body), max(chunk_size, 1)):
            yield self._body[start : start + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}", response=self)

    def close(self) -> None:
        return None


def urlopen_adapter(fake_urlopen: Callable[..., Any]) -> Callable[..., _AdaptedResponse]:
    """Return an ``origin_bounded_request`` stand-in backed by *fake_urlopen*."""

    def _origin_bounded_request(
        _session: object,
        method: str,
        url: str,
        *,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: object = None,
        **_kwargs: object,
    ) -> _AdaptedResponse:
        del base_url
        request = urllib.request.Request(url, headers=dict(headers or {}), method=method)  # noqa: S310
        try:
            body = fake_urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return _AdaptedResponse(exc.code, b"", url)
        except urllib.error.URLError as exc:
            raise requests.ConnectionError(str(exc.reason)) from exc
        return _AdaptedResponse(200, bytes(body.read()), url)

    return _origin_bounded_request


@contextlib.contextmanager
def patch_metadata_urlopen(**mock_kwargs: Any) -> Iterator[MagicMock]:
    """Patch the builder seam with a urllib-shaped ``MagicMock`` and yield it."""
    fake = MagicMock(**mock_kwargs)
    with patch(BUILDER_HTTP_SEAM, new=urlopen_adapter(fake)):
        yield fake
