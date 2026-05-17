"""Registry-backed package resolver.

Implements the install-side of docs/proposals/registry-api.md: given a
``DependencyReference`` whose ``source == "registry"``, fetch its tarball from
the configured registry, verify the sha256, extract into the target directory,
and build a ``PackageInfo`` that the rest of the install pipeline consumes
without further changes (per §8 the only new branch is the download itself).

This object satisfies the ``DownloadCallback`` shape used by the existing
resolver: it exposes a ``download_package(dep_ref, target_path, ...)`` method
that returns a ``PackageInfo``. Wiring into the install pipeline is done in
``install/phases/resolve.py`` (separate commit) — this module is a pure unit.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ...models.apm_package import PackageInfo, validate_apm_package
from ...models.dependency.reference import DependencyReference
from ...models.dependency.types import GitReferenceType, ResolvedReference
from .auth import (
    RegistryAuthContext,
    make_auth_context,
    remediation_message,
    resolve_for_url,
)
from .client import RegistryClient, RegistryError, VersionEntry
from .extractor import extract_archive
from .semver import is_semver_range, pick_best


class RegistryResolutionError(Exception):
    """A registry-sourced install failed in a way the user must act on.

    Wraps lower-level errors (auth, hash mismatch, no matching version, missing
    URL) into a single exception type the install pipeline can catch and
    surface with the §6.2 remediation message intact.
    """


def _split_owner_repo(repo_url: str) -> tuple[str, str]:
    """Split ``owner/repo`` (or longer paths) into (owner, repo) for the API.

    For paths with more than two segments (e.g. ``group/subgroup/repo``), we
    treat the last segment as the repo and the rest as the owner — the API
    contract uses ``{owner}/{repo}`` as a flat two-segment identity.
    """
    parts = [p for p in repo_url.split("/") if p]
    if len(parts) < 2:
        raise RegistryResolutionError(
            f"registry-sourced dep needs an 'owner/repo' identity, got {repo_url!r}"
        )
    if len(parts) == 2:
        return parts[0], parts[1]
    return "/".join(parts[:-1]), parts[-1]


def _clear_install_target(target_path: Path) -> None:
    """Ensure *target_path* is empty (mirrors Git downloader retry semantics)."""
    target_path.mkdir(parents=True, exist_ok=True)
    if not any(target_path.iterdir()):
        return
    for child in target_path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _package_info_from_extracted_registry_tree(
    dep_ref: DependencyReference,
    target_path: Path,
    chosen: VersionEntry,
    actual_hash: str,
    client: RegistryClient,
    owner: str,
    repo: str,
) -> tuple[PackageInfo, RegistryResolution]:
    """Validate extracted tree, attach registry URL, return install metadata."""
    if dep_ref.is_virtual and dep_ref.virtual_path and dep_ref.is_virtual_subdirectory():
        sub = target_path / dep_ref.virtual_path
        if not sub.exists():
            raise RegistryResolutionError(
                f"virtual sub-path {dep_ref.virtual_path!r} not found in "
                f"package {dep_ref.repo_url!r} at version {chosen.version}"
            )

    validation_result = validate_apm_package(target_path)
    if not validation_result.is_valid:
        errs = "\n  - ".join(validation_result.errors)
        raise RegistryResolutionError(
            f"registry tarball for {dep_ref.repo_url!r} did not validate "
            f"as an APM package:\n  - {errs}"
        )
    package = validation_result.package
    if package is None:
        raise RegistryResolutionError(
            f"registry tarball for {dep_ref.repo_url!r} validated but "
            f"produced no package metadata"
        )
    resolved_url = client.archive_url(owner, repo, chosen.version)
    package.source = resolved_url

    reg_resolution = RegistryResolution(
        resolved_url=resolved_url,
        resolved_hash=f"sha256:{actual_hash}",
        version=chosen.version,
    )

    resolved_ref = ResolvedReference(
        original_ref=dep_ref.reference or chosen.version,
        ref_type=GitReferenceType.TAG,
        ref_name=chosen.version,
        resolved_commit=None,
    )

    package_info = PackageInfo(
        package=package,
        install_path=target_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        package_type=validation_result.package_type,
    )
    return package_info, reg_resolution


class RegistryPackageResolver:
    """Drop-in download callback that fetches packages from a REST registry.

    One instance per resolution graph is fine — the resolver picks the right
    registry per dep based on ``dep_ref.registry_name`` and the configured
    ``registries`` mapping.
    """

    def __init__(
        self,
        registries: Dict[str, str],
        *,
        client_factory: Optional[
            Callable[[str, RegistryAuthContext], RegistryClient]
        ] = None,
    ) -> None:
        """
        Args:
            registries: Mapping of registry name -> base URL, sourced from the
                top-level ``registries:`` block in apm.yml (merged with user
                config). The ``"default"`` key, if present, may either be a
                string registry name or a string URL — it is consumed at
                routing time, not here, so this map should already be
                normalized to ``{name: url, ...}`` without ``"default"``.
            client_factory: Optional override for ``RegistryClient`` construction.
                Tests inject this to swap in a fake HTTP client.
        """
        self._registries = dict(registries)
        self._client_factory = client_factory or (
            lambda url, auth: RegistryClient(url, auth)
        )

    def _registry_call(
        self,
        dep_ref: DependencyReference,
        base_url: str,
        fn: Callable[[], Any],
    ) -> Any:
        """Run *fn*; map ``RegistryError`` to ``RegistryResolutionError``."""
        try:
            return fn()
        except RegistryError as exc:
            self._raise_for_http(exc, dep_ref, base_url)
            raise

    # The dep tracker stores a per-dep "resolution result" used by the
    # lockfile writer to fill in resolved_url + resolved_hash. It's exposed
    # via this lookup so the install pipeline can read it after the callback
    # returns. Keyed by ``DependencyReference.get_unique_key()``.
    @property
    def last_resolutions(self) -> Dict[str, "RegistryResolution"]:
        if not hasattr(self, "_last_resolutions"):
            self._last_resolutions = {}
        return self._last_resolutions

    # ------------------------------------------------------------------
    def _resolve_registry_url(self, registry_name: Optional[str]) -> str:
        if not registry_name:
            raise RegistryResolutionError(
                "registry-sourced dep is missing registry_name "
                "(parser bug or unconfigured default registry)"
            )
        url = self._registries.get(registry_name)
        if not url:
            raise RegistryResolutionError(
                f"registry {registry_name!r} is not configured in apm.yml's "
                f"registries: block"
            )
        return url

    def _build_client(self, registry_name: str, base_url: str) -> RegistryClient:
        auth = make_auth_context(registry_name)
        return self._client_factory(base_url, auth)

    def _build_client_for_url(self, base_url: str) -> RegistryClient:
        """Build a client for a URL whose registry name we look up from config.

        Used on the lockfile re-install path (§6.2): the URL is already
        recorded; we walk the configured registries to find which name owns
        that URL, then resolve its token. If no match, fall back to anonymous.
        """
        auth = resolve_for_url(base_url, self._registries)
        return self._client_factory(base_url, auth)

    def _pick_version(
        self, dep_ref: DependencyReference, versions: list[VersionEntry]
    ) -> VersionEntry:
        spec = dep_ref.reference or ""
        if not spec:
            raise RegistryResolutionError(
                f"registry-sourced dep {dep_ref.repo_url!r} has no version "
                f"constraint (semver range required)"
            )
        if not is_semver_range(spec):
            # The parser should have rejected this earlier; this is defense in
            # depth for direct callers that bypass the parser.
            raise RegistryResolutionError(
                f"version constraint {spec!r} on {dep_ref.repo_url!r} is not "
                f"a valid semver range"
            )
        version_strings = [v.version for v in versions]
        best = pick_best(spec, version_strings)
        if best is None:
            raise RegistryResolutionError(
                f"no version of {dep_ref.repo_url!r} matches {spec!r} "
                f"in registry {dep_ref.registry_name!r} "
                f"(available: {', '.join(version_strings) or '<none>'})"
            )
        chosen = next((v for v in versions if v.version == best), None)
        if chosen is None:
            raise RegistryResolutionError(
                f"internal error: picked {best!r} but no matching VersionEntry"
            )
        return chosen

    # ------------------------------------------------------------------
    def download_package(
        self,
        repo_ref,  # str | DependencyReference (mirrors GitHubPackageDownloader)
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
        verbose_callback=None,
    ) -> PackageInfo:
        """Fetch *repo_ref* from its configured registry into *target_path*.

        Mirrors ``GitHubPackageDownloader.download_package``'s signature so
        both can be invoked through the same ``DownloadCallback`` shape in
        ``install/phases/resolve.py``.
        """
        dep_ref = (
            repo_ref
            if isinstance(repo_ref, DependencyReference)
            else DependencyReference.parse(repo_ref)
        )
        if dep_ref.source != "registry":
            raise RegistryResolutionError(
                f"RegistryPackageResolver invoked for non-registry dep "
                f"{dep_ref.repo_url!r} (source={dep_ref.source!r})"
            )

        owner, repo = _split_owner_repo(dep_ref.repo_url)
        base_url = self._resolve_registry_url(dep_ref.registry_name)
        client = self._build_client(dep_ref.registry_name, base_url)

        versions = self._registry_call(
            dep_ref,
            base_url,
            lambda: client.list_versions(owner, repo),
        )
        if not versions:
            raise RegistryResolutionError(
                f"registry {dep_ref.registry_name!r} reports no versions for "
                f"{dep_ref.repo_url!r}"
            )

        chosen = self._pick_version(dep_ref, versions)

        archive_bytes, content_type = self._registry_call(
            dep_ref,
            base_url,
            lambda: client.download_archive(owner, repo, chosen.version),
        )

        _clear_install_target(target_path)

        # extract_archive dispatches on Content-Type (with magic-bytes
        # fallback) — supports both tar.gz (default) and zip (Anthropic
        # skills format). Hash check happens before any extraction.
        actual_hash = extract_archive(
            archive_bytes,
            chosen.digest,
            target_path,
            content_type=content_type,
        )

        # Subdirectory virtual packages: registry serves the parent tarball;
        # we extract the full tree then validate the requested sub-path exists.
        package_info, reg_resolution = _package_info_from_extracted_registry_tree(
            dep_ref,
            target_path,
            chosen,
            actual_hash,
            client,
            owner,
            repo,
        )
        self.last_resolutions[dep_ref.get_unique_key()] = reg_resolution
        return package_info

    # ------------------------------------------------------------------
    def _raise_for_http(
        self,
        exc: RegistryError,
        dep_ref: DependencyReference,
        base_url: str,
    ) -> None:
        if exc.status in (401, 403):
            raise RegistryResolutionError(
                f"{exc}\n{remediation_message(base_url)}"
            ) from exc
        if exc.status == 404:
            raise RegistryResolutionError(
                f"registry {dep_ref.registry_name!r} has no package "
                f"{dep_ref.repo_url!r} (HTTP 404 from {exc.url})"
            ) from exc
        raise RegistryResolutionError(str(exc)) from exc


# Carried out-of-band from download_package() so the install pipeline can
# read it when writing the lockfile (resolved_url + resolved_hash). Keeping
# the PackageInfo shape unchanged keeps existing callers byte-identical.
@dataclass(frozen=True, slots=True)
class RegistryResolution:
    resolved_url: str
    resolved_hash: str
    version: str

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"RegistryResolution(version={self.version!r}, "
            f"url={self.resolved_url!r}, hash={self.resolved_hash[:24]!r}...)"
        )
