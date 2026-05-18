"""Subdirectory package download operation for GitHubPackageDownloader.

Extracted from download_package_ops to keep individual modules ≤500 lines.
"""

import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from ...models.apm_package import (
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)
from ...utils.github_host import default_host
from ..shared_clone_cache import _RepoCoords
from .class_ import _close_repo, _rmtree
from .progress import GitProgressReporter


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
    # tempfile.TemporaryDirectory().__exit__ calls shutil.rmtree without our
    # retry logic, which raises WinError 32 when git processes still hold
    # handles at the end of the with-block.
    from ...config import get_apm_temp_dir

    temp_dir = None
    shared_bare_path: Path | None = None
    # WS2 path resolves the SHA from the BARE so we don't pay
    # rev-parse twice (or open the working-tree Repo unnecessarily).
    # See design.md sec 5.5: _ws2_resolved_commit threads the SHA past
    # the generic sys.modules[__package__].Repo(temp_clone_path).head.commit.hexsha block below.
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

            def _shared_bare_fetch_fn(existing_bare: Path, ref_or_sha: str) -> bool:
                # get_or_clone passes `ref` here; for SHA pins it is the SHA.
                return self._fetch_sha_into_bare(
                    existing_bare,
                    ref_or_sha,
                    dep_ref=dep_ref,
                )

            try:
                shared_bare_path = shared_cache.get_or_clone(
                    _RepoCoords(host=cache_host, owner=cache_owner, repo=cache_repo),
                    ref,
                    _shared_bare_clone_fn,
                    fetch_fn=_shared_bare_fetch_fn if is_commit_sha else None,
                )
            except Exception as e:
                raise RuntimeError(f"Failed to clone repository: {e}") from e

            # Per-consumer materialization. mkdtemp gives a unique
            # path so concurrent consumers do not collide. The bare
            # is read-only after this point; only the consumer dir
            # is written to.
            temp_dir = tempfile.mkdtemp(dir=get_apm_temp_dir())
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
            temp_dir = tempfile.mkdtemp(dir=get_apm_temp_dir())
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
                        repo_obj = sys.modules[__package__].Repo(temp_clone_path)
                        repo_obj.git.checkout(ref)
                    except Exception as e:
                        raise RuntimeError(f"Failed to checkout commit {ref}: {e}") from e
                    finally:
                        _close_repo(repo_obj)

                # Disable progress reporter after clone
                if progress_reporter:
                    progress_reporter.disabled = True

        # Update progress - extracting subdirectory
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=70, total=100)

        # Check if subdirectory exists
        source_subdir = temp_clone_path / subdir_path
        # Security: ensure subdirectory resolves within the cloned repo
        from ...utils.path_security import ensure_path_within

        ensure_path_within(source_subdir, temp_clone_path)
        if not source_subdir.exists():
            raise RuntimeError(f"Subdirectory '{subdir_path}' not found in repository")

        if not source_subdir.is_dir():
            raise RuntimeError(f"Path '{subdir_path}' is not a directory")

        # Create target directory
        target_path.mkdir(parents=True, exist_ok=True)

        # If target exists and has content, remove it
        if target_path.exists() and any(target_path.iterdir()):
            _rmtree(target_path)
            target_path.mkdir(parents=True, exist_ok=True)

        # Copy subdirectory contents to target (retry on transient
        # file-lock errors caused by antivirus scanning on Windows).
        from ...utils.file_ops import robust_copy2, robust_copytree

        for item in source_subdir.iterdir():
            src = source_subdir / item.name
            dst = target_path / item.name
            if src.is_dir():
                robust_copytree(src, dst)
            else:
                robust_copy2(src, dst)

        # Capture commit SHA; close the Repo object immediately so its file
        # handles are released before _rmtree() runs in the finally block.
        # WS2 path skips this because _materialize_from_bare already
        # resolved the SHA from the bare (avoids opening Repo on the
        # consumer dir, which leaks a Windows file handle that would
        # block the rmtree below; see design.md sec 5.5).
        if _ws2_resolved_commit is not None:
            resolved_commit = _ws2_resolved_commit
        else:
            repo = None
            try:
                repo = sys.modules[__package__].Repo(temp_clone_path)
                resolved_commit = repo.head.commit.hexsha
            except Exception:
                resolved_commit = "unknown"
            finally:
                _close_repo(repo)

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
            _rmtree(temp_dir)

    # Validate the extracted package (after temp dir is cleaned up)
    validation_result = sys.modules[__package__].validate_apm_package(target_path)
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
    from ..package_validator import stamp_plugin_version

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
