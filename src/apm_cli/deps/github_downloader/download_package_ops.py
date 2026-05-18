"""GitHub package downloader for APM dependencies."""

import sys
from datetime import datetime
from pathlib import Path
from typing import Union

from git.exc import GitCommandError

from ...models.apm_package import (
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    PackageType,
)
from ...utils.github_host import default_host

# Re-export so callers that do ``from . import download_package_ops as _m``
# can still access ``_m.download_subdirectory_package`` without change.
from ._subdir_package_ops import download_subdirectory_package  # noqa: F401
from .class_ import _rmtree
from .download_ops import ProgressCtx
from .progress import GitProgressReporter

_PROTOCOL_FALLBACK_DOCS_URL = (
    "https://microsoft.github.io/apm/guides/dependencies/#restoring-the-legacy-permissive-chain"
)


def _dispatch_virtual_package(
    self,
    dep_ref: "DependencyReference",
    target_path: Path,
    ctx: "ProgressCtx",
    art_proxy,
) -> "PackageInfo":
    """Route a virtual package to the appropriate download backend.

    Handles virtual-file, Artifactory-direct, Artifactory-proxy, and
    subdirectory cases.  The PROXY_REGISTRY_ONLY guard is enforced by
    the caller before invoking this helper.
    """
    if dep_ref.is_virtual_file():
        return self.download_virtual_file_package(
            dep_ref, target_path, ctx.progress_task_id, ctx.progress_obj
        )
    # SUBDIRECTORY (the only other virtual type after #1094 dropped
    # the `.collection.yml` form): includes Artifactory modes.
    if dep_ref.is_artifactory():
        proxy_info = (dep_ref.host, dep_ref.artifactory_prefix, "https")
        return self._download_subdirectory_from_artifactory(
            dep_ref, target_path, proxy_info, ctx.progress_task_id, ctx.progress_obj
        )
    if self._is_artifactory_only() and art_proxy:
        return self._download_subdirectory_from_artifactory(
            dep_ref, target_path, art_proxy, ctx.progress_task_id, ctx.progress_obj
        )
    return self.download_subdirectory_package(
        dep_ref, target_path, ctx.progress_task_id, ctx.progress_obj
    )


def download_package(
    self,
    repo_ref: Union[str, "DependencyReference"],
    target_path: Path,
    ctx: "ProgressCtx | None" = None,
) -> PackageInfo:
    """Download a GitHub repository and validate it as an APM package.

    For virtual packages (individual files or collections), creates a minimal
    package structure instead of cloning the full repository.

    Args:
        repo_ref: Repository reference — either a DependencyReference object
            or a string (e.g., "user/repo#branch"). Passing the object
            directly avoids a lossy parse round-trip for generic git hosts.
        target_path: Local path where package should be downloaded
        ctx: Bundled progress/callback context (progress_task_id, progress_obj,
            verbose_callback). Defaults to a no-op ProgressCtx when omitted.

    Returns:
        PackageInfo: Information about the downloaded package

    Raises:
        ValueError: If the repository reference is invalid
        RuntimeError: If download or validation fails
    """
    if ctx is None:
        ctx = ProgressCtx()
    progress_task_id = ctx.progress_task_id
    progress_obj = ctx.progress_obj
    verbose_callback = ctx.verbose_callback
    # Accept both string and DependencyReference to avoid lossy round-trips
    if isinstance(repo_ref, DependencyReference):
        dep_ref = repo_ref
    else:
        try:
            dep_ref = DependencyReference.parse(repo_ref)
        except ValueError as e:
            raise ValueError(f"Invalid repository reference '{repo_ref}': {e}") from e

    # Handle virtual packages differently
    if dep_ref.is_virtual:
        art_proxy = self._parse_artifactory_base_url()
        if self._is_artifactory_only() and not dep_ref.is_artifactory() and not art_proxy:
            raise RuntimeError(
                f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{repo_ref}'. "
                "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
            )
        return _dispatch_virtual_package(self, dep_ref, target_path, ctx, art_proxy)

    # Artifactory download path (Mode 1: explicit FQDN, Mode 2: transparent proxy)
    use_artifactory = dep_ref.is_artifactory()
    art_proxy = None
    if not use_artifactory:
        art_proxy = self._parse_artifactory_base_url()
        if art_proxy and self._should_use_artifactory_proxy(dep_ref):
            use_artifactory = True

    if use_artifactory:
        return self._download_package_from_artifactory(
            dep_ref, target_path, art_proxy, progress_task_id, progress_obj
        )

    # When PROXY_REGISTRY_ONLY is set but no Artifactory proxy matched, block direct git
    if self._is_artifactory_only():
        raise RuntimeError(
            f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{dep_ref}'. "
            "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
        )

    # Regular package download (existing logic)
    resolved_ref = self.resolve_git_reference(dep_ref)

    # Create target directory if it doesn't exist
    target_path.mkdir(parents=True, exist_ok=True)

    # If directory already exists and has content, remove it
    if target_path.exists() and any(target_path.iterdir()):
        _rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)

    # WS3 (#1116): persistent cross-run cache fast path for whole-repo
    # deps.  When a cached checkout exists for the resolved SHA, copy
    # files directly into target_path and skip the network clone.
    _persistent_cache = self.persistent_git_cache
    if _persistent_cache is not None:
        try:
            cache_host = dep_ref.host or default_host()
            cache_owner = dep_ref.repo_url.split("/")[0] if "/" in dep_ref.repo_url else ""
            cache_repo = (
                dep_ref.repo_url.split("/")[1] if "/" in dep_ref.repo_url else dep_ref.repo_url
            )
            _canonical_url = f"https://{cache_host}/{cache_owner}/{cache_repo}"
            _cached = _persistent_cache.get_checkout(
                _canonical_url,
                resolved_ref.resolved_commit or resolved_ref.ref_name,
                locked_sha=resolved_ref.resolved_commit,
                env=self._git_env_dict(),
            )
            from ...utils.file_ops import robust_copy2, robust_copytree

            for item in _cached.iterdir():
                if item.name == ".git":
                    continue
                src = _cached / item.name
                dst = target_path / item.name
                if src.is_dir():
                    robust_copytree(src, dst)
                else:
                    robust_copy2(src, dst)

            # Validate, then return without cloning.
            validation_result = sys.modules[__package__].validate_apm_package(target_path)
            if validation_result.is_valid and validation_result.package:
                package = validation_result.package
                package.source = dep_ref.to_github_url()
                package.resolved_commit = resolved_ref.resolved_commit
                if (
                    validation_result.package_type == PackageType.MARKETPLACE_PLUGIN
                    and package.version == "0.0.0"
                    and resolved_ref.resolved_commit
                ):
                    short_sha = resolved_ref.resolved_commit[:7]
                    package.version = short_sha
                    apm_yml_path = target_path / "apm.yml"
                    if apm_yml_path.exists():
                        from ...utils.yaml_io import dump_yaml, load_yaml

                        _data = load_yaml(apm_yml_path) or {}
                        _data["version"] = short_sha
                        dump_yaml(_data, apm_yml_path)
                return PackageInfo(
                    package=package,
                    install_path=target_path,
                    resolved_reference=resolved_ref,
                    installed_at=datetime.now().isoformat(),
                    dependency_ref=dep_ref,
                    package_type=validation_result.package_type,
                )
            # Validation failed against cached copy: fall through to a
            # fresh clone (cache may be stale or repo structure changed).
            if target_path.exists() and any(target_path.iterdir()):
                _rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Any cache failure -> fall back to network clone.
            if target_path.exists() and any(target_path.iterdir()):
                _rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)

    # Store progress reporter so we can disable it after clone
    progress_reporter = None
    package_display_name = (
        dep_ref.repo_url.split("/")[-1] if "/" in dep_ref.repo_url else dep_ref.repo_url
    )

    try:
        # Clone the repository using fallback authentication methods
        # Use shallow clone for performance if we have a specific commit
        if resolved_ref.ref_type == GitReferenceType.COMMIT:
            # For commits, we need to clone and checkout the specific commit
            progress_reporter = (
                GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                if progress_task_id and progress_obj
                else None
            )
            repo = self._clone_with_fallback(
                dep_ref.repo_url,
                target_path,
                progress_reporter=progress_reporter,
                dep_ref=dep_ref,
                verbose_callback=verbose_callback,
            )
            repo.git.checkout(resolved_ref.resolved_commit)
        else:
            # For branches and tags, we can use shallow clone
            progress_reporter = (
                GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                if progress_task_id and progress_obj
                else None
            )
            repo = self._clone_with_fallback(
                dep_ref.repo_url,
                target_path,
                progress_reporter=progress_reporter,
                dep_ref=dep_ref,
                verbose_callback=verbose_callback,
                depth=1,
                branch=resolved_ref.ref_name,
            )

        # Disable progress reporter to prevent late git updates
        if progress_reporter:
            progress_reporter.disabled = True

        # Remove .git directory to save space and prevent treating as a Git repository
        git_dir = target_path / ".git"
        if git_dir.exists():
            _rmtree(git_dir)

    except GitCommandError as e:
        # Check if this might be a private repository access issue
        if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
            error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
            host = dep_ref.host or default_host()
            org = dep_ref.repo_url.split("/")[0] if dep_ref.repo_url else None
            error_msg += self.auth_resolver.build_error_context(
                host,
                "clone",
                org=org,
                port=dep_ref.port,
                dep_url=dep_ref.repo_url,
            )
            raise RuntimeError(error_msg) from e
        else:
            sanitized_error = self._sanitize_git_error(str(e))
            raise RuntimeError(
                f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}"
            ) from e
    except RuntimeError:
        # Re-raise RuntimeError from _clone_with_fallback
        raise

    # Validate the downloaded package
    validation_result = sys.modules[__package__].validate_apm_package(target_path)
    if not validation_result.is_valid:
        # Clean up on validation failure
        if target_path.exists():
            _rmtree(target_path)

        error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
        for error in validation_result.errors:
            error_msg += f"  - {error}\n"
        raise RuntimeError(error_msg.strip())

    # Load the APM package metadata
    if not validation_result.package:
        raise RuntimeError(
            f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}"
        )

    package = validation_result.package
    package.source = dep_ref.to_github_url()
    package.resolved_commit = resolved_ref.resolved_commit

    # For plugins without an explicit version, use the short commit SHA so the
    # lock file and conflict detection have a meaningful, stable version string.
    from ..package_validator import stamp_plugin_version

    stamp_plugin_version(
        package,
        validation_result.package_type,
        resolved_ref.resolved_commit,
        target_path,
    )

    # Create and return PackageInfo
    return PackageInfo(
        package=package,
        install_path=target_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,  # Store for canonical dependency string
        package_type=validation_result.package_type,  # Track if APM, Claude Skill, or Hybrid
    )
