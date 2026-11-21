"""A recording in-memory GitHub API for the merge-queue runner tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from scripts.merge_queue.api import Response, with_query


class FakeApi:
    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.post_handler: Callable[[str, Mapping[str, Any]], Response] = lambda p, b: Response(
            201, {"sha": "f" * 40}
        )
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = with_query(path, params)
        self.calls.append(("GET", url, None))
        parts = urlsplit(url)
        key = parts.path
        if key not in self.routes:
            raise KeyError(f"unrouted GET {key} {parse_qs(parts.query)}")
        value = self.routes[key]
        return value(parse_qs(parts.query)) if callable(value) else value

    def post(self, path: str, body: Mapping[str, Any]) -> Response:
        self.calls.append(("POST", path, dict(body)))
        return self.post_handler(path, body)

    def patch(self, path: str, body: Mapping[str, Any]) -> Response:
        self.calls.append(("PATCH", path, dict(body)))
        return Response(200, {})

    def delete(self, path: str) -> Response:
        self.calls.append(("DELETE", path, None))
        return Response(204, None)

    def queries(self, path: str) -> list[dict[str, list[str]]]:
        return [
            parse_qs(urlsplit(url).query)
            for method, url, _ in self.calls
            if method == "GET" and urlsplit(url).path == path
        ]
