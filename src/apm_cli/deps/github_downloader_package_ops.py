"""Whole-repo / virtual-file / sparse-checkout ops for :class:`GitHubPackageDownloader`.

Moved bodies (kept thin wrappers on the class): ``download_package`` and its
``_package_*`` helpers, ``download_virtual_file_package`` and its
``_virtual_*`` helpers, and ``try_sparse_checkout``. Cross-cluster calls
(e.g. routing a virtual dep to the subdirectory handler) go through the
class wrappers via ``downloader.<method>`` so they stay monkeypatch-safe and
form no import cycle. Patched globals are routed through ``_gh.<name>``.
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Union

from git.exc import GitCommandError

from ..models.apm_package import (
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)


def download_virtual_file_package(
    downloader,
    dep_ref: DependencyReference,
    target_path: Path,
    progress_task_id=None,
    progress_obj=None,
) -> PackageInfo:
    """Download a single file as a virtual APM package.

    Creates a minimal APM package structure with the file placed in the
    appropriate .apm/ subdirectory based on its extension.
    """
    from apm_cli.deps import github_downloader as _gh

    if not dep_ref.is_virtual or not dep_ref.virtual_path:
        raise ValueError("Dependency must be a virtual file package")

    if not dep_ref.is_virtual_file():
        raise ValueError(
            f"Path '{dep_ref.virtual_path}' is not a valid individual file. "
            f"Must end with one of: {', '.join(DependencyReference.VIRTUAL_FILE_EXTENSIONS)}"
        )

    # Determine the ref to use
    ref = dep_ref.reference or "main"

    # Resolve the commit SHA cheaply BEFORE the file download (one short HTTP
    # call). On non-GitHub hosts or any failure this returns None and we fall
    # back to ref-name only -- the install never fails on SHA resolution.
    resolved_commit = downloader._resolve_commit_sha_for_ref(dep_ref, ref)

    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=50, total=100)

    try:
        file_content = downloader.download_raw_file(dep_ref, dep_ref.virtual_path, ref)
    except RuntimeError as e:
        raise RuntimeError(f"Failed to download virtual package: {e}") from e

    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=90, total=100)

    target_path.mkdir(parents=True, exist_ok=True)

    subdir = _virtual_subdir_for(dep_ref.virtual_path)
    if not subdir:
        raise ValueError(f"Unknown file extension for {dep_ref.virtual_path}")

    filename = dep_ref.virtual_path.split("/")[-1]
    apm_dir = target_path / ".apm" / subdir
    apm_dir.mkdir(parents=True, exist_ok=True)

    file_path = apm_dir / filename
    file_path.write_bytes(file_content)

    package_name = dep_ref.get_virtual_package_name()
    description = _virtual_description(file_content, filename)

    apm_yml_data = {
        "name": package_name,
        "version": "1.0.0",
        "description": description,
        "author": dep_ref.repo_url.split("/")[0],
    }
    apm_yml_content = _gh.yaml_to_str(apm_yml_data)

    apm_yml_path = target_path / "apm.yml"
    apm_yml_path.write_text(apm_yml_content, encoding="utf-8")

    package = _gh.APMPackage(
        name=package_name,
        version="1.0.0",
        description=description,
        author=dep_ref.repo_url.split("/")[0],
        source=dep_ref.to_github_url(),
        package_path=target_path,
    )

    # Build the resolved reference. On non-GitHub hosts or SHA-resolve failure
    # the resolved_commit stays None and the suffix renders as "#ref" only.
    ref_type = (
        GitReferenceType.COMMIT
        if re.match(r"^[a-f0-9]{40}$", ref.lower())
        else GitReferenceType.BRANCH
    )
    resolved_ref = ResolvedReference(
        original_ref=str(dep_ref.reference) if dep_ref.reference else ref,
        ref_name=ref,
        ref_type=ref_type,
        resolved_commit=resolved_commit,
    )

    return PackageInfo(
        package=package,
        install_path=target_path,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        resolved_reference=resolved_ref,
    )


def _virtual_subdir_for(virtual_path: str) -> str | None:
    """Map a virtual file path's extension to its .apm subdirectory."""
    subdirs = {
        ".prompt.md": "prompts",
        ".instructions.md": "instructions",
        ".chatmode.md": "chatmodes",
        ".agent.md": "agents",
    }
    for ext, dir_name in subdirs.items():
        if virtual_path.endswith(ext):
            return dir_name
    return None


def _virtual_description(file_content: bytes, filename: str) -> str:
    """Extract a description from YAML frontmatter, or build a default one."""
    description = f"Virtual package containing {filename}"
    try:
        content_str = file_content.decode("utf-8")
        if content_str.startswith("---\n"):
            end_idx = content_str.find("\n---\n", 4)
            if end_idx > 0:
                frontmatter = content_str[4:end_idx]
                for line in frontmatter.split("\n"):
                    if line.startswith("description:"):
                        return line.split(":", 1)[1].strip().strip("\"'")
    except Exception:
        # If frontmatter parsing fails, use the default description.
        pass
    return description


def try_sparse_checkout(
    downloader,
    dep_ref: DependencyReference,
    temp_clone_path: Path,
    subdir_path: str,
    ref: str | None = None,
) -> bool:
    """Attempt sparse-checkout to download only a subdirectory (git 2.25+).

    Returns True on success. Falls back silently on failure.
    """
    from apm_cli.deps import github_downloader as _gh

    try:
        temp_clone_path.mkdir(parents=True, exist_ok=True)

        # Resolve per-dependency auth via AuthResolver.
        dep_auth_ctx = downloader._resolve_dep_auth_ctx(dep_ref)
        dep_token = dep_auth_ctx.token if dep_auth_ctx else downloader.github_token
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        # For ADO bearer, use the AuthContext git_env with header injection.
        if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
            env = {**os.environ, **(dep_auth_ctx.git_env or {})}
        else:
            env = {**os.environ, **(downloader.git_env or {})}
        auth_url = downloader._build_repo_url(
            dep_ref.repo_url,
            use_ssh=False,
            dep_ref=dep_ref,
            token=dep_token,
            auth_scheme=dep_auth_scheme,
        )

        cmds = [
            ["git", "init"],
            ["git", "remote", "add", "origin", auth_url],
            ["git", "sparse-checkout", "init", "--cone"],
            ["git", "sparse-checkout", "set", subdir_path],
        ]
        fetch_cmd = ["git", "fetch", "origin"]
        fetch_cmd.append(ref or "HEAD")
        fetch_cmd.append("--depth=1")
        cmds.append(fetch_cmd)
        cmds.append(["git", "checkout", "FETCH_HEAD"])

        for cmd in cmds:
            result = _gh.subprocess.run(
                cmd,
                cwd=str(temp_clone_path),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            if result.returncode != 0:
                _gh._debug(
                    f"Sparse-checkout step failed ({' '.join(cmd)}): {result.stderr.strip()}"
                )
                return False

        return True
    except Exception as e:
        _gh._debug(f"Sparse-checkout failed: {e}")
        return False


def download_package(
    downloader,
    repo_ref: Union[str, "DependencyReference"],
    target_path: Path,
    progress_task_id=None,
    progress_obj=None,
    verbose_callback=None,
) -> PackageInfo:
    """Download a GitHub repository and validate it as an APM package.

    For virtual packages (individual files or subdirectories), creates a
    minimal package structure / extracts the subdir instead of cloning the
    full repository. Artifactory FQDN/proxy modes route to the Artifactory
    orchestrator. A persistent cross-run cache fast-path may skip the clone.
    """
    from apm_cli.deps import github_downloader as _gh

    # Accept both string and DependencyReference to avoid lossy round-trips.
    if isinstance(repo_ref, DependencyReference):
        dep_ref = repo_ref
    else:
        try:
            dep_ref = DependencyReference.parse(repo_ref)
        except ValueError as e:
            raise ValueError(f"Invalid repository reference '{repo_ref}': {e}") from e

    # Handle virtual packages differently.
    if dep_ref.is_virtual:
        return _package_download_virtual(
            downloader, dep_ref, repo_ref, target_path, progress_task_id, progress_obj
        )

    # Artifactory download path (Mode 1: explicit FQDN, Mode 2: transparent proxy).
    use_artifactory = dep_ref.is_artifactory()
    art_proxy = None
    if not use_artifactory:
        art_proxy = downloader._parse_artifactory_base_url()
        if art_proxy and downloader._should_use_artifactory_proxy(dep_ref):
            use_artifactory = True

    if use_artifactory:
        return downloader._download_package_from_artifactory(
            dep_ref, target_path, art_proxy, progress_task_id, progress_obj
        )

    # PROXY_REGISTRY_ONLY set but no Artifactory proxy matched -> block direct git.
    if downloader._is_artifactory_only():
        raise RuntimeError(
            f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{dep_ref}'. "
            "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
        )

    # Regular package download.
    resolved_ref = downloader.resolve_git_reference(dep_ref)

    target_path.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and any(target_path.iterdir()):
        _gh._rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)

    # WS3: persistent cross-run cache fast path for whole-repo deps.
    if downloader.persistent_git_cache is not None:
        cached = _package_try_persistent_cache(downloader, dep_ref, resolved_ref, target_path)
        if cached is not None:
            return cached

    _package_clone_repo(
        downloader,
        dep_ref,
        resolved_ref,
        target_path,
        progress_task_id,
        progress_obj,
        verbose_callback,
    )
    return _package_finalize(downloader, dep_ref, resolved_ref, target_path)


def _package_download_virtual(
    downloader, dep_ref, repo_ref, target_path, progress_task_id, progress_obj
) -> PackageInfo:
    """Route a virtual dep to file / subdirectory / Artifactory handlers."""
    art_proxy = downloader._parse_artifactory_base_url()
    if downloader._is_artifactory_only() and not dep_ref.is_artifactory() and not art_proxy:
        raise RuntimeError(
            f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{repo_ref}'. "
            "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
        )
    if dep_ref.is_virtual_file():
        return downloader.download_virtual_file_package(
            dep_ref, target_path, progress_task_id, progress_obj
        )
    # SUBDIRECTORY (the only other virtual type): includes Artifactory modes.
    if dep_ref.is_artifactory():
        proxy_info = (dep_ref.host, dep_ref.artifactory_prefix, "https")
        return downloader._download_subdirectory_from_artifactory(
            dep_ref, target_path, proxy_info, progress_task_id, progress_obj
        )
    if downloader._is_artifactory_only() and art_proxy:
        return downloader._download_subdirectory_from_artifactory(
            dep_ref, target_path, art_proxy, progress_task_id, progress_obj
        )
    return downloader.download_subdirectory_package(
        dep_ref, target_path, progress_task_id, progress_obj
    )


def _package_try_persistent_cache(downloader, dep_ref, resolved_ref, target_path):
    """Copy a cached checkout into target_path, validate, and build PackageInfo.

    Returns the PackageInfo on a usable cache hit, or None to fall through to a
    fresh network clone (cache miss, stale copy, or validation failure).
    """
    from apm_cli.deps import github_downloader as _gh

    persistent_cache = downloader.persistent_git_cache
    try:
        cache_host = dep_ref.host or _gh.default_host()
        cache_owner = dep_ref.repo_url.split("/")[0] if "/" in dep_ref.repo_url else ""
        cache_repo = dep_ref.repo_url.split("/")[1] if "/" in dep_ref.repo_url else dep_ref.repo_url
        canonical_url = f"https://{cache_host}/{cache_owner}/{cache_repo}"
        cached = persistent_cache.get_checkout(
            canonical_url,
            resolved_ref.resolved_commit or resolved_ref.ref_name,
            locked_sha=resolved_ref.resolved_commit,
            env=downloader._git_env_dict(),
        )
        from ..utils.file_ops import robust_copy2, robust_copytree

        for item in cached.iterdir():
            if item.name == ".git":
                continue
            src = cached / item.name
            dst = target_path / item.name
            if src.is_dir():
                robust_copytree(src, dst)
            else:
                robust_copy2(src, dst)

        validation_result = _gh.validate_apm_package(target_path)
        if validation_result.is_valid and validation_result.package:
            return _package_info_from_cache(validation_result, dep_ref, resolved_ref, target_path)
        # Validation failed against cached copy: fall through to a fresh clone.
        _package_clean_target(target_path)
    except Exception:
        # Any cache failure -> fall back to network clone.
        _package_clean_target(target_path)
    return None


def _package_info_from_cache(validation_result, dep_ref, resolved_ref, target_path) -> PackageInfo:
    """Build PackageInfo from a validated cached checkout (stamping plugin version)."""
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
            from ..utils.yaml_io import dump_yaml, load_yaml

            data = load_yaml(apm_yml_path) or {}
            data["version"] = short_sha
            dump_yaml(data, apm_yml_path)
    return PackageInfo(
        package=package,
        install_path=target_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        package_type=validation_result.package_type,
    )


def _package_clean_target(target_path) -> None:
    """Remove target_path contents so a fresh clone starts clean."""
    from apm_cli.deps import github_downloader as _gh

    if target_path.exists() and any(target_path.iterdir()):
        _gh._rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)


def _package_clone_repo(
    downloader,
    dep_ref,
    resolved_ref,
    target_path,
    progress_task_id,
    progress_obj,
    verbose_callback,
) -> None:
    """Clone the repo (shallow for branches/tags, checkout for commits) and drop .git."""
    from apm_cli.deps import github_downloader as _gh

    progress_reporter = None
    package_display_name = (
        dep_ref.repo_url.split("/")[-1] if "/" in dep_ref.repo_url else dep_ref.repo_url
    )

    try:
        if resolved_ref.ref_type == GitReferenceType.COMMIT:
            progress_reporter = (
                _gh.GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                if progress_task_id and progress_obj
                else None
            )
            repo = downloader._clone_with_fallback(
                dep_ref.repo_url,
                target_path,
                progress_reporter=progress_reporter,
                dep_ref=dep_ref,
                verbose_callback=verbose_callback,
            )
            repo.git.checkout(resolved_ref.resolved_commit)
        else:
            progress_reporter = (
                _gh.GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                if progress_task_id and progress_obj
                else None
            )
            repo = downloader._clone_with_fallback(
                dep_ref.repo_url,
                target_path,
                progress_reporter=progress_reporter,
                dep_ref=dep_ref,
                verbose_callback=verbose_callback,
                depth=1,
                branch=resolved_ref.ref_name,
            )

        if progress_reporter:
            progress_reporter.disabled = True

        # Remove .git to save space and prevent treating target as a Git repo.
        git_dir = target_path / ".git"
        if git_dir.exists():
            _gh._rmtree(git_dir)
    except GitCommandError as e:
        _package_raise_clone_error(downloader, dep_ref, e)
    except RuntimeError:
        # Re-raise RuntimeError from _clone_with_fallback.
        raise


def _package_raise_clone_error(downloader, dep_ref, e: GitCommandError) -> None:
    """Translate a GitCommandError into an actionable RuntimeError."""
    from apm_cli.deps import github_downloader as _gh

    if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
        error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
        host = dep_ref.host or _gh.default_host()
        org = dep_ref.repo_url.split("/")[0] if dep_ref.repo_url else None
        error_msg += downloader.auth_resolver.build_error_context(
            host,
            "clone",
            org=org,
            port=dep_ref.port,
            dep_url=dep_ref.repo_url,
        )
        raise RuntimeError(error_msg) from e
    sanitized_error = downloader._sanitize_git_error(str(e))
    raise RuntimeError(f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}") from e


def _package_finalize(downloader, dep_ref, resolved_ref, target_path) -> PackageInfo:
    """Validate the cloned package, stamp version, and build PackageInfo."""
    from apm_cli.deps import github_downloader as _gh

    from ._shared import _validate_and_load_package
    from .package_validator import stamp_plugin_version

    validation_result = _gh.validate_apm_package(target_path)
    package = _validate_and_load_package(validation_result, target_path, dep_ref)
    package.resolved_commit = resolved_ref.resolved_commit

    # For plugins without an explicit version, use the short commit SHA.
    stamp_plugin_version(
        package,
        validation_result.package_type,
        resolved_ref.resolved_commit,
        target_path,
    )

    return PackageInfo(
        package=package,
        install_path=target_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        package_type=validation_result.package_type,
    )
