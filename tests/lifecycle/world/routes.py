"""Scripted routes, the enterprise registry proxy and proxy-only mode (#4094).

- ``script`` / ``redirect``: answer a host and path prefix with a scripted
  response, for example a 3xx to another host or path. The spare hosts in
  ``MIRROR_HOSTS`` exist so a redirect can leave the world's usual origins.
- ``RegistryProxy``: an Artifactory-shaped ``PROXY_REGISTRY_URL`` serving
  GitHub-style zip archives (``{repo}-{ref}/`` root) of the world's git
  repositories, bearer token required.
- ``forbid_direct``: the "unproxied route" mode. Every direct content host
  (git, APIs, raw, marketplace) answers 403 and is still recorded, so a test
  proves ``PROXY_REGISTRY_ONLY`` never goes direct. Sanctioned routes stay
  reachable: org policy repositories (policy fetches are direct by design),
  the operator-configured MCP registry (``MCP_REGISTRY_URL``) and the package
  registry bound in user config.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from tests.utils.local_scripted_https_proxy import ScriptedRequest, ScriptedResponse

PROXY_HOST = "proxy.lifecycle.test"
PROXY_PREFIX = "artifactory/github"
PROXY_URL = f"https://{PROXY_HOST}/{PROXY_PREFIX}"
PROXY_TOKEN = "lifecycle-proxy-token-81b2"
MIRROR_HOSTS = ("mirror.lifecycle.test", "elsewhere.lifecycle.test")
_ARCHIVE_PATHS = ("/archive/refs/heads/", "/archive/refs/tags/", "/zip/refs/heads/")


@dataclass(frozen=True)
class ScriptedRoute:
    host: str
    path_prefix: str
    response: ScriptedResponse


@dataclass
class Routes:
    """Per-test routing state; ``clear`` runs on every ``WorldServices.reset``."""

    scripted: list[ScriptedRoute] = field(default_factory=list)
    direct_forbidden: bool = False

    def clear(self) -> None:
        self.scripted.clear()
        self.direct_forbidden = False

    def match(self, request: ScriptedRequest) -> ScriptedResponse | None:
        path = unquote(request.path)
        for route in reversed(self.scripted):
            if request.host == route.host and path.startswith(route.path_prefix):
                return route.response
        return None


def forbidden_direct() -> ScriptedResponse:
    return ScriptedResponse(
        403, {"Content-Type": "text/plain"}, b"direct route forbidden: proxy-only world"
    )


def _git_archive(repository: Path, ref: str, prefix: str) -> bytes | None:
    git = shutil.which("git") or "git"
    result = subprocess.run(
        (git, "--git-dir", str(repository), "archive", "--format=zip", f"--prefix={prefix}", ref),
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else None


@dataclass
class RegistryProxy:
    """``https://proxy.lifecycle.test/artifactory/github/{owner}/{repo}/archive/...``."""

    lookup: Callable[[str, str], Path | None]

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        if request.header("Authorization") != f"Bearer {PROXY_TOKEN}":
            return ScriptedResponse(401, {"Content-Type": "text/plain"}, b"Unauthorized")
        path = unquote(request.path)
        base = f"/{PROXY_PREFIX}/"
        kind = next((k for k in _ARCHIVE_PATHS if k in path), None)
        if not path.startswith(base) or kind is None:
            return ScriptedResponse(404)
        slug, _, archive = path[len(base) :].partition(kind)
        owner, _, repo = slug.partition("/")
        ref = archive.removesuffix(".zip")
        repository = self.lookup(owner, repo)
        if repository is None or not ref:
            return ScriptedResponse(404)
        body = _git_archive(repository, ref, f"{repo}-{ref}/")
        if body is None:
            return ScriptedResponse(404)
        return ScriptedResponse(200, {"Content-Type": "application/zip"}, body)
