"""The GitHub REST boundary. Everything else in the runner takes an ``Api``."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

REPO = "microsoft/apm"
PER_PAGE = 100
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
_STATUS_LINE = re.compile(r"^HTTP/[\d.]+ (\d{3})")


@dataclass(frozen=True)
class Response:
    status: int
    body: Any


class Api(Protocol):
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...

    def post(self, path: str, body: Mapping[str, Any]) -> Response: ...

    def delete(self, path: str) -> Response: ...


def with_query(path: str, params: Mapping[str, Any] | None) -> str:
    return f"{path}?{urlencode(params)}" if params else path


def get_all(api: Api, path: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    """Every page of a list endpoint, combined (stops at the first short page)."""
    items: list[Any] = []
    page = 1
    while True:
        batch = api.get(path, {**(params or {}), "per_page": PER_PAGE, "page": page}) or []
        items.extend(batch)
        if len(batch) < PER_PAGE:
            return items
        page += 1


def parse_included(stdout: str) -> Response:
    """Split ``gh api --include`` output into the real HTTP status and the JSON body."""
    head, _, body = stdout.replace("\r\n", "\n").partition("\n\n")
    match = _STATUS_LINE.match(head)
    status = int(match.group(1)) if match else 0
    return Response(status, json.loads(body) if body.strip() else None)


class GhApi:
    """``gh api`` with the caller's credentials; no shell, fixed argv."""

    def _run(self, args: list[str], body: Mapping[str, Any] | None = None) -> Response:
        result = subprocess.run(  # noqa: S603
            ["gh", "api", "--include", *args],  # noqa: S607
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return parse_included(result.stdout)
        match = _HTTP_STATUS.search(result.stderr)
        return Response(int(match.group(1)) if match else 0, result.stderr.strip())

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        response = self._run([with_query(path, params)])
        if response.status != 200:
            raise RuntimeError(f"GET {path} failed ({response.status}): {response.body}")
        return response.body

    def post(self, path: str, body: Mapping[str, Any]) -> Response:
        return self._run(["-X", "POST", path, "--input", "-"], body)

    def delete(self, path: str) -> Response:
        return self._run(["-X", "DELETE", path])
