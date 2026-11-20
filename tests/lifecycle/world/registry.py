"""Package registry of the lifecycle world (#4094).

Serves the REST contract of ``src/apm_cli/deps/registry/client.py`` on
``https://registry.lifecycle.test/apm`` through the world proxy:

- ``GET /v1/packages/{owner}/{repo}/versions`` lists versions with the sha256
  digest of each archive, so the lock records ``resolved_hash``;
- ``GET /v1/packages/{owner}/{repo}/versions/{version}/download`` serves the
  zip archive.

The registry is private: every request needs ``Authorization: Bearer <TOKEN>``
and anything else gets 401. The world env carries the token
(``TOKEN_ENV``) and the template's user config binds ``NAME`` to ``URL``, the
origin binding APM requires before it presents a registry credential.
``use_registry`` (``scopes.py``) makes the registry a manifest's default, so
the shorthand spec ``lifecycle/p-skill#1.0.0`` resolves through it.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from tests.utils.local_scripted_https_proxy import ScriptedRequest, ScriptedResponse

HOST = "registry.lifecycle.test"
BASE = "/apm"
URL = f"https://{HOST}{BASE}"
NAME = "lifecycle-registry"
TOKEN = "lifecycle-registry-token-5f3a9c1e"
TOKEN_ENV = "APM_REGISTRY_TOKEN_LIFECYCLE_REGISTRY"
PUBLISHED_AT = "2026-11-01T00:00:00Z"
# Package name -> versions the template publishes (oldest first).
REGISTRY_PUBLISHED: dict[str, tuple[str, ...]] = {
    "p-skill": ("1.0.0", "1.1.0"),
    "p-exec": ("1.0.0",),
}
_PREFIX = f"{BASE}/v1/packages/"
# 1980-01-01, the zip epoch: archives are byte-identical across builds.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def zip_tree(root: Path) -> bytes:
    """A deterministic zip of every file under *root* (POSIX names, exec bits kept)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), _ZIP_EPOCH)
            info.external_attr = (0o755 if path.stat().st_mode & 0o111 else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def digest(archive: bytes) -> str:
    return "sha256:" + hashlib.sha256(archive).hexdigest()


def _json(status: int, payload: object) -> ScriptedResponse:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    return ScriptedResponse(status, {"Content-Type": "application/json"}, body)


@dataclass
class PackageRegistry:
    """Archives by ``owner/repo`` and version; one instance per world services."""

    archives: dict[str, dict[str, bytes]] = field(default_factory=dict)

    def publish(self, slug: str, version: str, archive: bytes) -> str:
        """Publish *archive* as *slug* at *version*; return its ``sha256:`` digest."""
        self.archives.setdefault(slug, {})[version] = archive
        return digest(archive)

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        if request.header("Authorization") != f"Bearer {TOKEN}":
            return _json(401, {"title": "Unauthorized", "status": 401})
        path = unquote(request.path).rstrip("/")
        if request.method != "GET" or not path.startswith(_PREFIX):
            return _json(404, {"title": "Not Found", "status": 404})
        parts = path[len(_PREFIX) :].split("/")
        versions = self.archives.get("/".join(parts[:2]))
        if versions is None or len(parts) < 3 or parts[2] != "versions":
            return _json(404, {"title": "Not Found", "status": 404})
        if len(parts) == 3:
            rows = [
                {"version": v, "digest": digest(a), "published_at": PUBLISHED_AT}
                for v, a in versions.items()
            ]
            return _json(200, {"versions": rows})
        if len(parts) == 5 and parts[4] == "download" and parts[3] in versions:
            return ScriptedResponse(200, {"Content-Type": "application/zip"}, versions[parts[3]])
        return _json(404, {"title": "Not Found", "status": 404})
