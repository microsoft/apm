"""APM-controlled loopback implementation of the package Registry HTTP API.

Implements the three endpoints documented in
``docs/src/content/docs/reference/registry-http-api.md`` section 3 as a
handler for :func:`tests.utils.local_scripted_https_proxy.local_scripted_https_proxy`:

* ``GET  {base}/v1/packages/{owner}/{repo}/versions``
* ``GET  {base}/v1/packages/{owner}/{repo}/versions/{version}/download``
* ``PUT  {base}/v1/packages/{owner}/{repo}/versions/{version}``

Artifacts are keyed by ``(package, version)`` and the download endpoint
serves exactly the bytes that were uploaded with ``PUT`` (or seeded with
:meth:`LocalPackageRegistry.seed`). Versions are immutable: a second ``PUT``
for the same key answers ``409``. ``download_fault`` lets a scenario simulate
an artifact outage or a registry that serves the wrong bytes (optionally a
well-formed ``substitute_archive``) without changing the advertised digest.

No vendor service, account or SDK is involved.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote

from tests.utils.local_scripted_https_proxy import ScriptedRequest, ScriptedResponse

_PUBLISHED_AT = "2026-01-01T00:00:00Z"
_ARCHIVE_TYPES = ("application/zip", "application/gzip")

DownloadFault = Literal["outage", "wrong-bytes"]


def sha256_digest(data: bytes) -> str:
    """Return the ``sha256:<hex>`` digest the Registry HTTP API advertises."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def flat_registry_archive(files: Iterable[tuple[str, str]]) -> bytes:
    """Return a deterministic flat registry zip containing *files*."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, text in files:
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            package.writestr(info, text)
    return buffer.getvalue()


@dataclass(frozen=True)
class RegistryArtifact:
    """One immutable published version."""

    package: str
    version: str
    data: bytes
    content_type: str = "application/zip"

    @property
    def digest(self) -> str:
        return sha256_digest(self.data)


def _json(status: int, payload: object, content_type: str = "application/json") -> ScriptedResponse:
    return ScriptedResponse(status, {"Content-Type": content_type}, json.dumps(payload).encode())


def _problem(status: int, title: str, instance: str) -> ScriptedResponse:
    payload = {"title": title, "status": status, "instance": instance}
    return _json(status, payload, "application/problem+json")


@dataclass
class LocalPackageRegistry:
    """In-memory Registry HTTP API origin rooted at ``base_path``."""

    base_path: str = "/apm"
    artifacts: dict[tuple[str, str], RegistryArtifact] = field(default_factory=dict)
    uploads: list[RegistryArtifact] = field(default_factory=list)
    download_fault: DownloadFault | None = None
    substitute_archive: bytes | None = None

    def seed(self, package: str, version: str, data: bytes) -> RegistryArtifact:
        """Pre-publish *data* as *package* at *version* without a ``PUT``."""
        artifact = RegistryArtifact(package, version, data)
        self.artifacts[(package, version)] = artifact
        return artifact

    def artifact(self, package: str, version: str) -> RegistryArtifact:
        return self.artifacts[(package, version)]

    def download_path(self, package: str, version: str) -> str:
        return f"{self.base_path}/v1/packages/{package}/versions/{version}/download"

    @property
    def published(self) -> list[bytes]:
        """Bodies accepted through ``PUT``, in upload order."""
        return [artifact.data for artifact in self.uploads]

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        """Answer one request addressed to this registry origin."""
        path = request.path.rstrip("/")
        prefix = f"{self.base_path}/v1/packages/"
        if not path.startswith(prefix):
            return _problem(404, "Not found", path)
        tail = unquote(path[len(prefix) :])
        package, separator, rest = tail.partition("/versions")
        if not separator or not package:
            return _problem(404, "Not found", path)
        if rest == "" and request.method in ("GET", "HEAD"):
            return self._versions(package, path)
        version, _, action = rest.lstrip("/").partition("/")
        if not version:
            return _problem(404, "Not found", path)
        if action == "" and request.method == "PUT":
            return self._publish(package, version, request, path)
        if action == "download" and request.method in ("GET", "HEAD"):
            return self._download(package, version, path)
        return _problem(405 if action in ("", "download") else 404, "Not allowed", path)

    def _versions(self, package: str, path: str) -> ScriptedResponse:
        entries = [a for (name, _), a in self.artifacts.items() if name == package]
        if not entries:
            return _problem(404, "Package not found", path)
        versions = [
            {
                "version": artifact.version,
                "digest": artifact.digest,
                "published_at": _PUBLISHED_AT,
                "size_bytes": len(artifact.data),
            }
            for artifact in reversed(entries)
        ]
        return _json(200, {"package": package, "versions": versions})

    def _publish(
        self, package: str, version: str, request: ScriptedRequest, path: str
    ) -> ScriptedResponse:
        if (package, version) in self.artifacts:
            return _problem(409, "Version already published", path)
        content_type = (request.header("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in _ARCHIVE_TYPES:
            return _problem(415, "Unsupported archive type", path)
        if not request.body:
            return _problem(400, "Empty archive", path)
        artifact = RegistryArtifact(package, version, request.body, content_type)
        self.artifacts[(package, version)] = artifact
        self.uploads.append(artifact)
        payload = {
            "package": package,
            "version": version,
            "digest": artifact.digest,
            "published_at": _PUBLISHED_AT,
            "size_bytes": len(artifact.data),
        }
        return _json(201, payload)

    def _download(self, package: str, version: str, path: str) -> ScriptedResponse:
        artifact = self.artifacts.get((package, version))
        if artifact is None:
            return _problem(404, "Version not found", path)
        if self.download_fault == "outage":
            return _problem(503, "Artifact storage unavailable", path)
        data = artifact.data
        if self.download_fault == "wrong-bytes":
            data = self.substitute_archive or data + b"\x00tampered"
        return ScriptedResponse(200, {"Content-Type": artifact.content_type}, data)
