"""Package download methods for GitHubPackageDownloader."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from git.exc import GitCommandError

from ..models.apm_package import (
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)
from ..utils.github_host import default_host
from . import github_downloader as _compat
from .github_progress import GitProgressReporter


def _try_sparse_checkout(
    self,
    dep_ref: DependencyReference,
    temp_clone_path: Path,
    subdir_path: str,
    ref: str | None = None,
) -> bool:
    """Attempt sparse-checkout to download only a subdirectory (git 2.25+).

    Returns True on success. Falls back silently on failure.
    """

    try:
        temp_clone_path.mkdir(parents=True, exist_ok=True)

        # Resolve per-dependency token via AuthResolver.
        dep_token = self._resolve_dep_token(dep_ref)
        dep_auth_ctx = self._resolve_dep_auth_ctx(dep_ref)
        dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

        # For ADO bearer, use the AuthContext git_env with header injection
        if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
            env = {**os.environ, **(dep_auth_ctx.git_env or {})}
        else:
            env = {**os.environ, **(self.git_env or {})}
        auth_url = self._build_repo_url(
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
            result = subprocess.run(
                cmd,
                cwd=str(temp_clone_path),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            if result.returncode != 0:
                safe_cmd = [
                    _compat.sanitize_token_url_in_message(part, host=default_host()) for part in cmd
                ]
                _compat._debug(
                    f"Sparse-checkout step failed ({' '.join(safe_cmd)}): {result.stderr.strip()}"
                )
                return False

        return True
    except Exception as e:
        _compat._debug(f"Sparse-checkout failed: {e}")
        return False


def download_subdirectory_package(
    self,
    dep_ref: DependencyReference,
    target_path: Path,
    progress_task_id=None,
    progress_obj=None,
) -> PackageInfo:
    """Download a subdirectory from a repo as an APM package.

    Used for Claude Skills or APM packages nested in monorepos.
    Clones the repo, extracts the subdirectory, and cleans up.

    Args:
        dep_ref: Dependency reference with virtual_path set to subdirectory
        target_path: Local path where package should be created
        progress_task_id: Rich Progress task ID for progress updates
        progress_obj: Rich Progress object for progress updates

    Returns:
        PackageInfo: Information about the downloaded package

    Raises:
        ValueError: If the dependency is not a valid subdirectory package
        RuntimeError: If download or validation fails
    """
    if not dep_ref.is_virtual or not dep_ref.virtual_path:
        raise ValueError("Dependency must be a virtual subdirectory package")

    if not dep_ref.is_virtual_subdirectory():
        raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid subdirectory package")

    # Use user-specified ref, or None to use repo's default branch
    ref = dep_ref.reference  # None if not specified
    subdir_path = dep_ref.virtual_path

    # Update progress - starting
    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=10, total=100)

    # WS2a (#1116): attempt shared clone dedup when a per-run cache
    # is available.  Two subdir deps from the same (host, owner, repo, ref)
    # share one clone; different refs always get independent clones.
    shared_cache = self.shared_clone_cache
    use_shared = shared_cache is not None
    # Determine cache key components from the dep_ref.
    cache_host = dep_ref.host or default_host()
    cache_owner = dep_ref.repo_url.split("/")[0] if "/" in dep_ref.repo_url else ""
    cache_repo = dep_ref.repo_url.split("/")[1] if "/" in dep_ref.repo_url else dep_ref.repo_url

    # WS3 (#1116): try persistent cross-run cache first.
    # Build a canonical URL for cache key derivation.
    _persistent_cache = self.persistent_git_cache
    _persistent_checkout: Path | None = None
    if _persistent_cache is not None:
        _canonical_url = f"https://{cache_host}/{cache_owner}/{cache_repo}"
        try:
            _persistent_checkout = _persistent_cache.get_checkout(
                _canonical_url, ref, env=self._git_env_dict()
            )
        except Exception:
            # Cache miss or failure -- fall through to normal clone path.
            _persistent_checkout = None

    # Use mkdtemp + explicit cleanup so we control when rmtree runs.
    # _compat.tempfile.TemporaryDirectory().__exit__ calls shutil.rmtree without our
    # retry logic, which raises WinError 32 when git processes still hold
    # handles at the end of the with-block.
    from ..config import get_apm_temp_dir

    temp_dir = None
    shared_bare_path: Path | None = None
    # WS2 path resolves the SHA from the BARE so we don't pay
    # rev-parse twice (or open the working-tree Repo unnecessarily).
    # See design.md sec 5.5: _ws2_resolved_commit threads the SHA past
    # the generic _compat.Repo(temp_clone_path).head.commit.hexsha block below.
    _ws2_resolved_commit: str | None = None
    try:
        if _persistent_checkout is not None:
            # WS3: persistent cache hit -- use the cached checkout directly.
            temp_clone_path = _persistent_checkout
        elif use_shared:
            # WS2 (#1126): shared cache holds BARE clones keyed by
            # (host, owner, repo, ref). Each consumer materializes its
            # own working tree from the bare; this is subdir-agnostic
            # so two parallel consumers requesting different
            # subdirectories of the same repo+ref can share one bare
            # without racing on sparse-checkout. See design.md sec 5.5.
            is_commit_sha = ref and re.match(r"^[a-f0-9]{7,40}$", ref) is not None

            def _shared_bare_clone_fn(bare_target: Path) -> None:
                self._bare_clone_with_fallback(
                    dep_ref.repo_url,
                    bare_target,
                    dep_ref=dep_ref,
                    ref=ref,
                    is_commit_sha=bool(is_commit_sha),
                )

            try:
                shared_bare_path = shared_cache.get_or_clone(
                    cache_host, cache_owner, cache_repo, ref, _shared_bare_clone_fn
                )
            except Exception as e:
                raise RuntimeError(f"Failed to clone repository: {e}") from e

            # Per-consumer materialization. mkdtemp gives a unique
            # path so concurrent consumers do not collide. The bare
            # is read-only after this point; only the consumer dir
            # is written to.
            temp_dir = _compat.tempfile.mkdtemp(dir=get_apm_temp_dir())
            temp_clone_path = Path(temp_dir) / "consumer"
            try:
                _ws2_resolved_commit = self._materialize_from_bare(
                    shared_bare_path,
                    temp_clone_path,
                    ref=ref,
                    env=self._git_env_dict(),
                    # Only short-circuit SHA resolution when the user
                    # pinned a full 40-char SHA. Abbreviated SHAs
                    # (7-39 chars) must be resolved to the full
                    # SHA against the bare so resolved_commit
                    # matches `head.commit.hexsha` (always 40-char)
                    # in lockfile comparisons. The bare's HEAD has
                    # already been update-ref'd to the full SHA in
                    # _bare_action, so rev-parse HEAD returns 40 chars.
                    # Copilot review finding (#1135).
                    known_sha=ref if (is_commit_sha and len(ref) == 40) else None,
                )
            except Exception as e:
                raise RuntimeError(f"Failed to prepare dependency from cached clone: {e}") from e
        else:
            # Legacy per-dep clone path (no shared cache).
            temp_dir = _compat.tempfile.mkdtemp(dir=get_apm_temp_dir())
            # Sparse checkout always targets "repo/".  If it fails we clone into
            # "repo_clone/" so we never have to rmtree a directory that may still
            # have live git handles from the failed subprocess.
            sparse_clone_path = Path(temp_dir) / "repo"
            temp_clone_path = sparse_clone_path

            # Update progress - cloning
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=20, total=100)

            # Phase 4 (#171): Try sparse-checkout first (git 2.25+), fall back to full clone
            sparse_ok = self._try_sparse_checkout(dep_ref, sparse_clone_path, subdir_path, ref)

            if not sparse_ok:
                # Full clone into a fresh subdirectory so we don't have to touch
                # the (possibly locked) sparse-checkout directory at all.
                temp_clone_path = Path(temp_dir) / "repo_clone"

                package_display_name = subdir_path.split("/")[-1]
                progress_reporter = (
                    GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                    if progress_task_id and progress_obj
                    else None
                )

                # Detect if ref is a commit SHA (can't be used with --branch in shallow clones)
                is_commit_sha = ref and re.match(r"^[a-f0-9]{7,40}$", ref) is not None

                clone_kwargs = {
                    "dep_ref": dep_ref,
                }
                if is_commit_sha:
                    # For commit SHAs, clone without checkout then checkout the specific commit.
                    # Shallow clone doesn't support fetching by arbitrary SHA.
                    clone_kwargs["no_checkout"] = True
                else:
                    clone_kwargs["depth"] = 1
                    if ref:
                        clone_kwargs["branch"] = ref

                try:
                    self._clone_with_fallback(
                        dep_ref.repo_url,
                        temp_clone_path,
                        progress_reporter=progress_reporter,
                        **clone_kwargs,
                    )
                except Exception as e:
                    raise RuntimeError(f"Failed to clone repository: {e}") from e

                if is_commit_sha:
                    repo_obj = None
                    try:
                        repo_obj = _compat.Repo(temp_clone_path)
                        repo_obj.git.checkout(ref)
                    except Exception as e:
                        raise RuntimeError(f"Failed to checkout commit {ref}: {e}") from e
                    finally:
                        _compat._close_repo(repo_obj)

                # Disable progress reporter after clone
                if progress_reporter:
                    progress_reporter.disabled = True

        # Update progress - extracting subdirectory
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=70, total=100)

        # Check if subdirectory exists
        source_subdir = temp_clone_path / subdir_path
        # Security: ensure subdirectory resolves within the cloned repo
        from ..utils.path_security import ensure_path_within

        ensure_path_within(source_subdir, temp_clone_path)
        if not source_subdir.exists():
            raise RuntimeError(f"Subdirectory '{subdir_path}' not found in repository")

        if not source_subdir.is_dir():
            raise RuntimeError(f"Path '{subdir_path}' is not a directory")

        # Create target directory
        target_path.mkdir(parents=True, exist_ok=True)

        # If target exists and has content, remove it
        if target_path.exists() and any(target_path.iterdir()):
            _compat._rmtree(target_path)
            target_path.mkdir(parents=True, exist_ok=True)

        # Copy subdirectory contents to target (retry on transient
        # file-lock errors caused by antivirus scanning on Windows).
        from ..utils.file_ops import robust_copy2, robust_copytree

        for item in source_subdir.iterdir():
            src = source_subdir / item.name
            dst = target_path / item.name
            if src.is_dir():
                robust_copytree(src, dst)
            else:
                robust_copy2(src, dst)

        # Capture commit SHA; close the Repo object immediately so its file
        # handles are released before _compat._rmtree() runs in the finally block.
        # WS2 path skips this because _materialize_from_bare already
        # resolved the SHA from the bare (avoids opening Repo on the
        # consumer dir, which leaks a Windows file handle that would
        # block the rmtree below; see design.md sec 5.5).
        if _ws2_resolved_commit is not None:
            resolved_commit = _ws2_resolved_commit
        else:
            repo = None
            try:
                repo = _compat.Repo(temp_clone_path)
                resolved_commit = repo.head.commit.hexsha
            except Exception:
                resolved_commit = "unknown"
            finally:
                _compat._close_repo(repo)

        # Update progress - validating
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=90, total=100)

    except PermissionError as exc:
        exc_path = getattr(exc, "filename", None)
        # If temp_dir wasn't created (mkdtemp failed) or the error is within
        # the temp tree, this is likely a restricted temp directory issue.
        if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
            raise RuntimeError(
                "Access denied in temporary directory"
                + (f" '{temp_dir}'" if temp_dir else "")
                + ". Corporate security may restrict this path. "
                "Fix: apm config set temp-dir <WRITABLE_PATH>"
            ) from None
        raise
    except OSError as exc:
        if getattr(exc, "errno", None) == 13 or getattr(exc, "winerror", None) == 5:
            exc_path = getattr(exc, "filename", None)
            if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
                raise RuntimeError(
                    "Access denied in temporary directory"
                    + (f" '{temp_dir}'" if temp_dir else "")
                    + ". Corporate security may restrict this path. "
                    "Fix: apm config set temp-dir <WRITABLE_PATH>"
                ) from None
        raise
    finally:
        if temp_dir:
            _compat._rmtree(temp_dir)

    # Validate the extracted package (after temp dir is cleaned up)
    validation_result = _compat.validate_apm_package(target_path)
    if not validation_result.is_valid:
        error_msgs = "; ".join(validation_result.errors)
        raise RuntimeError(f"Subdirectory is not a valid APM package or Claude Skill: {error_msgs}")

    # Get the resolved reference for metadata
    resolved_ref = ResolvedReference(
        original_ref=ref or "default",
        ref_name=ref or "default",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=resolved_commit,
    )

    # For plugins without an explicit version, stamp with the short commit SHA.
    package = validation_result.package
    from .package_validator import stamp_plugin_version

    stamp_plugin_version(
        package,
        validation_result.package_type,
        resolved_commit,
        target_path,
    )

    # Update progress - complete
    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=100, total=100)

    return PackageInfo(
        package=package,
        install_path=target_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        dependency_ref=dep_ref,
        package_type=validation_result.package_type,
    )


def _download_subdirectory_from_artifactory(
    self,
    dep_ref: DependencyReference,
    target_path: Path,
    proxy_info: tuple,
    progress_task_id=None,
    progress_obj=None,
) -> PackageInfo:
    """Backward-compat stub -- delegates to ArtifactoryOrchestrator."""
    return self._artifactory.download_subdirectory(
        dep_ref,
        target_path,
        proxy_info,
        progress_task_id=progress_task_id,
        progress_obj=progress_obj,
    )


def _download_package_from_artifactory(
    self,
    dep_ref: DependencyReference,
    target_path: Path,
    proxy_info: tuple | None = None,
    progress_task_id=None,
    progress_obj=None,
) -> PackageInfo:
    """Backward-compat stub -- delegates to ArtifactoryOrchestrator."""
    return self._artifactory.download_package(
        dep_ref,
        target_path,
        proxy_info=proxy_info,
        progress_task_id=progress_task_id,
        progress_obj=progress_obj,
    )


def download_package(
    self,
    repo_ref: str | DependencyReference,
    target_path: Path,
    progress_task_id=None,
    progress_obj=None,
    verbose_callback=None,
) -> PackageInfo:
    """Download a GitHub repository and validate it as an APM package.

    For virtual packages (individual files or collections), creates a minimal
    package structure instead of cloning the full repository.

    Args:
        repo_ref: Repository reference Ã¢â‚¬â€ either a DependencyReference object
            or a string (e.g., "user/repo#branch"). Passing the object
            directly avoids a lossy parse round-trip for generic git hosts.
        target_path: Local path where package should be downloaded
        progress_task_id: Rich Progress task ID for progress updates
        progress_obj: Rich Progress object for progress updates
        verbose_callback: Optional callable for verbose logging (receives str messages)

    Returns:
        PackageInfo: Information about the downloaded package

    Raises:
        ValueError: If the repository reference is invalid
        RuntimeError: If download or validation fails
    """
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
        if dep_ref.is_virtual_file():
            return self.download_virtual_file_package(
                dep_ref, target_path, progress_task_id, progress_obj
            )
        # SUBDIRECTORY (the only other virtual type after #1094 dropped
        # the `.collection.yml` form): includes Artifactory modes.
        if dep_ref.is_artifactory():
            proxy_info = (dep_ref.host, dep_ref.artifactory_prefix, "https")
            return self._download_subdirectory_from_artifactory(
                dep_ref, target_path, proxy_info, progress_task_id, progress_obj
            )
        if self._is_artifactory_only() and art_proxy:
            return self._download_subdirectory_from_artifactory(
                dep_ref, target_path, art_proxy, progress_task_id, progress_obj
            )
        return self.download_subdirectory_package(
            dep_ref, target_path, progress_task_id, progress_obj
        )

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
        _compat._rmtree(target_path)
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
            from ..utils.file_ops import robust_copy2, robust_copytree

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
            validation_result = _compat.validate_apm_package(target_path)
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
                        from ..utils.yaml_io import dump_yaml, load_yaml

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
                _compat._rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Any cache failure -> fall back to network clone.
            if target_path.exists() and any(target_path.iterdir()):
                _compat._rmtree(target_path)
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
            _compat._rmtree(git_dir)

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
    validation_result = _compat.validate_apm_package(target_path)
    if not validation_result.is_valid:
        # Clean up on validation failure
        if target_path.exists():
            _compat._rmtree(target_path)

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
    from .package_validator import stamp_plugin_version

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


def _get_clone_progress_callback(self):
    """Get a progress callback for Git clone operations.

    Returns:
        Callable that can be used as progress callback for GitPython
    """

    def progress_callback(op_code, cur_count, max_count=None, message=""):
        """Progress callback for Git operations."""
        if max_count:
            percentage = int((cur_count / max_count) * 100)
            print(
                f"\r Cloning: {percentage}% ({cur_count}/{max_count}) {message}",
                end="",
                flush=True,
            )
        else:
            print(f"\r Cloning: {message} ({cur_count})", end="", flush=True)

    return progress_callback
