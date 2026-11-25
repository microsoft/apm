"""Hermetic twins and mirror shims of the enterprise journeys E02-E06 (#4110).

The enterprise journeys mostly install the world's own packages
(``lifecycle/p-skill``, ``lifecycle/p-exec``); ``TWINS_ENTERPRISE`` adds two
skill packages for E04's Azure DevOps and GitLab hosts.
``publish_enterprise_twins`` mirrors ``publish_twins`` for the runner and also
installs the mirrors below.

E03 (proxy-only and air-gapped) needs a registry proxy (an Artifactory-style
archive mirror) and a release metadata mirror. The world's recording proxy
terminates TLS only for the hosts it minted a certificate for, so
``install_enterprise_mirror`` serves both mirrors under one of those hosts
(``MIRROR_HOST``) and delegates every other request on it to the world handler.
E06 (experimental registries) needs a Registry HTTP API server; it is served
under the same host. Every mirror request still lands in ``services.egress_log()``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tests.lifecycle.journeys.twins import RELEASE_TAG, Twin, _md
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_scripted_https_proxy import ScriptedRequest, ScriptedResponse


def _skill_twin(name: str) -> Twin:
    """A one-skill package served on every world host shape (E04)."""
    return Twin(
        f"lifecycle/{name}",
        "v1.0.0",
        {
            "apm.yml": f"name: {name}\nversion: 1.0.0\ndescription: Enterprise journey package.\n",
            f"skills/{name}/SKILL.md": _md(
                f"name: {name}\ndescription: Enterprise journey skill {name}.\n", f"Use {name}."
            ),
        },
    )


# E04 installs one package per private host shape; distinct names keep their skills apart.
TWINS_ENTERPRISE: Mapping[str, Twin] = {
    twin.slug: twin for twin in (_skill_twin("e-ado-skill"), _skill_twin("e-gitlab-skill"))
}

# A host the world proxy already terminates TLS for and E03 does not use otherwise.
MIRROR_HOST = "ghes.lifecycle.test"
REGISTRY_PREFIX = "artifactory/github"
REGISTRY_PROXY_URL = f"https://{MIRROR_HOST}/{REGISTRY_PREFIX}"
RELEASE_METADATA_URL = f"https://{MIRROR_HOST}/generic/apm-releases/latest.json"
# A Registry HTTP API v1 server (reference/registry-http-api.md): every world
# repository is published at version 1.0.0 as a flat archive of its v1.0.0 tag.
REGISTRY_URL = f"https://{MIRROR_HOST}/registry/corp-main"
REGISTRY_VERSION = "1.0.0"
_PUBLISHED_AT = "2026-01-01T00:00:00Z"


def publish_enterprise_twins(services, root: Path, env: Mapping[str, str]) -> dict[str, Path]:
    """Publish every enterprise twin under *root* and register it with *services*."""
    factory = LocalGitRepositoryFactory(root / "repositories", env=env)
    published: dict[str, Path] = {}
    for slug, twin in TWINS_ENTERPRISE.items():
        tree = root / "trees" / slug.replace("/", "__")
        for relative, text in twin.files.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="ascii")
        repository = factory.create(slug.replace("/", "__"), source_tree=tree)
        commit = factory.commit(repository, message=f"{slug} {twin.ref}")
        if twin.ref != "main":
            factory.tag(repository, twin.ref, commit)
        published[slug] = repository.origin
    services._repositories.update(published)
    install_enterprise_mirror(services)
    return published


def _archive(repository: Path, ref: str, top: str) -> bytes | None:
    git = shutil.which("git") or "git"
    result = subprocess.run(
        (git, "--git-dir", str(repository), "archive", "--format=zip", f"--prefix={top}", ref),
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else None


def _blob(repository: Path, ref: str, path: str) -> bytes | None:
    git = shutil.which("git") or "git"
    result = subprocess.run(
        (git, "--git-dir", str(repository), "cat-file", "blob", f"{ref}:{path}"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.stdout if result.returncode == 0 else None


def _archive_ref(rest: list[str]) -> str | None:
    """Git ref of an archive path (GitHub, codeload or bare-commit shapes)."""
    if len(rest) > 3 and rest[0] in ("archive", "zip") and rest[1] == "refs":
        name = "/".join(rest[3:]).removesuffix(".zip")
        return {"heads": f"refs/heads/{name}", "tags": f"refs/tags/{name}"}.get(rest[2])
    if len(rest) == 2 and rest[0] in ("archive", "zip"):
        return rest[1].removesuffix(".zip")
    return None


def _registry_response(services, path: str) -> ScriptedResponse:
    parts = path[len(urlsplit(REGISTRY_URL).path) :].strip("/").split("/")
    missing = _json(404, {"title": "Not Found", "status": 404})
    if len(parts) < 5 or parts[:2] != ["v1", "packages"] or parts[4] != "versions":
        return missing
    owner, repo = unquote(parts[2]), unquote(parts[3])
    repository = services._repositories.get(f"{owner}/{repo}")
    if repository is None:
        return missing
    content = _archive(repository, "refs/tags/v1.0.0", "")
    if content is None:
        return missing
    if len(parts) == 5:
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        version = {"version": REGISTRY_VERSION, "digest": digest, "published_at": _PUBLISHED_AT}
        return _json(200, {"package": f"{owner}/{repo}", "versions": [version]})
    if parts[5:] == [REGISTRY_VERSION, "download"]:
        return ScriptedResponse(200, {"Content-Type": "application/zip"}, content)
    return missing


def _json(status: int, payload: object) -> ScriptedResponse:
    body = json.dumps(payload).encode("ascii")
    return ScriptedResponse(status, {"Content-Type": "application/json"}, body)


def _mirror_response(services, request: ScriptedRequest) -> ScriptedResponse | None:
    path = unquote(urlsplit(request.target).path)
    if path.startswith(urlsplit(REGISTRY_URL).path + "/"):
        return _registry_response(services, urlsplit(request.target).path)
    if path == urlsplit(RELEASE_METADATA_URL).path:
        body = json.dumps({"tag_name": RELEASE_TAG}).encode("ascii")
        return ScriptedResponse(200, {"Content-Type": "application/json"}, body)
    prefix = f"/{REGISTRY_PREFIX}/"
    if not path.startswith(prefix):
        return None
    archive_path, _, entry = path[len(prefix) :].partition("!/")
    parts = archive_path.split("/")
    missing = ScriptedResponse(404, {}, b"")
    if len(parts) < 4:
        return missing
    owner, repo = parts[0], parts[1]
    repository = services._repositories.get(f"{owner}/{repo}")
    ref = _archive_ref(parts[2:])
    if repository is None or ref is None:
        return missing
    top = f"{repo}-{ref.rsplit('/', 1)[-1]}"
    if entry:
        head, _, inner = entry.partition("/")
        content = _blob(repository, ref, inner) if head == top else None
        kind = "application/octet-stream"
    else:
        content = _archive(repository, ref, f"{top}/")
        kind = "application/zip"
    if content is None:
        return missing
    return ScriptedResponse(200, {"Content-Type": kind}, content)


def install_enterprise_mirror(services) -> None:
    """Serve the registry proxy and release metadata mirror under ``MIRROR_HOST``."""
    handlers = services.proxy._server.handlers
    original = handlers[MIRROR_HOST]
    if getattr(original, "enterprise_mirror", False):
        return

    def handler(request: ScriptedRequest) -> ScriptedResponse:
        response = _mirror_response(services, request)
        return response if response is not None else original(request)

    handler.enterprise_mirror = True  # type: ignore[attr-defined]
    handlers[MIRROR_HOST] = handler
