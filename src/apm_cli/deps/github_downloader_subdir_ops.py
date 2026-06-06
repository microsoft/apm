"""Subdirectory-package ops for :class:`GitHubPackageDownloader`.

Moved body (kept a thin wrapper on the class): ``download_subdirectory_package``
decomposed per cache tier (persistent WS3, shared-bare WS2, legacy
sparse/plain-clone fallback). The tiers are intentionally distinct and must
not be merged. Patched globals are routed through a function-level
``from apm_cli.deps import github_downloader as _gh`` alias.
"""

import re
from datetime import datetime
from pathlib import Path

from ..models.apm_package import (
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)


class _SubdirCloneState:
    """Mutable lifecycle holder so a tier helper can register its temp dir.

    ``temp_dir`` must be visible to the orchestrator's ``finally`` block the
    moment a helper creates it (so a mid-clone failure still cleans up).
    ``ws2_resolved_commit`` carries the SHA the bare-cache path already
    resolved, letting the extract step skip re-opening the working tree.
    """

    __slots__ = ("temp_dir", "ws2_resolved_commit")

    def __init__(self):
        self.temp_dir = None
        self.ws2_resolved_commit = None


def download_subdirectory_package(
    downloader,
    dep_ref: DependencyReference,
    target_path: Path,
    progress_task_id=None,
    progress_obj=None,
) -> PackageInfo:
    """Download a subdirectory from a repo as an APM package.

    Used for Claude Skills or APM packages nested in monorepos. Clones the
    repo (through whichever cache tier applies), extracts the subdirectory,
    validates it, and cleans up.

    The cache tiers are intentionally distinct and must not be merged:
    persistent cross-run cache (WS3), per-run shared bare clone (WS2), and the
    legacy per-dep sparse/full clone fallback. Each is its own helper.
    """
    from apm_cli.deps import github_downloader as _gh

    if not dep_ref.is_virtual or not dep_ref.virtual_path:
        raise ValueError("Dependency must be a virtual subdirectory package")

    if not dep_ref.is_virtual_subdirectory():
        raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid subdirectory package")

    # Use user-specified ref, or None to use repo's default branch
    ref = dep_ref.reference
    subdir_path = dep_ref.virtual_path
    perf_logger = getattr(downloader, "install_logger", None)
    dep_display = str(dep_ref)

    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=10, total=100)

    shared_cache = downloader.shared_clone_cache
    use_shared = shared_cache is not None
    cache_host = dep_ref.host or _gh.default_host()
    cache_owner = dep_ref.repo_url.split("/")[0] if "/" in dep_ref.repo_url else ""
    cache_repo = dep_ref.repo_url.split("/")[1] if "/" in dep_ref.repo_url else dep_ref.repo_url

    # WS3: try persistent cross-run cache first.
    persistent_checkout: Path | None = None
    if downloader.persistent_git_cache is not None:
        persistent_checkout = _subdir_persistent_checkout(
            downloader, dep_ref, ref, subdir_path, cache_host, cache_owner, cache_repo
        )

    state = _SubdirCloneState()
    try:
        if persistent_checkout is not None:
            # WS3: persistent cache hit -- use the cached checkout directly.
            temp_clone_path = persistent_checkout
            _subdir_log_persistent_hit(
                perf_logger, dep_display, ref, subdir_path, persistent_checkout
            )
        elif use_shared:
            temp_clone_path = _subdir_shared_bare_materialize(
                downloader,
                dep_ref,
                ref,
                subdir_path,
                cache_host,
                cache_owner,
                cache_repo,
                shared_cache,
                perf_logger,
                dep_display,
                state,
            )
        else:
            temp_clone_path = _subdir_legacy_clone(
                downloader, dep_ref, ref, subdir_path, progress_task_id, progress_obj, state
            )

        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=70, total=100)

        resolved_commit = _subdir_extract_to_target(
            downloader, temp_clone_path, subdir_path, target_path, state.ws2_resolved_commit
        )

        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=90, total=100)
    except PermissionError as exc:
        _subdir_reraise_access_error(exc, state.temp_dir)
        raise
    except OSError as exc:
        if getattr(exc, "errno", None) == 13 or getattr(exc, "winerror", None) == 5:
            _subdir_reraise_access_error(exc, state.temp_dir)
        raise
    finally:
        if state.temp_dir:
            _gh._rmtree(state.temp_dir)

    return _subdir_build_package_info(
        target_path, ref, resolved_commit, dep_ref, progress_task_id, progress_obj
    )


def _subdir_persistent_checkout(
    downloader, dep_ref, ref, subdir_path, cache_host, cache_owner, cache_repo
) -> Path | None:
    """WS3: resolve a sparse-keyed checkout from the persistent cross-run cache."""
    persistent_cache = downloader.persistent_git_cache
    canonical_url = f"https://{cache_host}/{cache_owner}/{cache_repo}"
    try:
        # Tiered ref resolution (#1433): resolve the ref BEFORE get_checkout so
        # the cache skips its internal ls-remote (same pattern as the non-subdir
        # path which passes locked_sha=resolved).
        try:
            resolved_sha = downloader.resolve_git_reference(dep_ref).resolved_commit
        except Exception:
            resolved_sha = None
        # Sparse-cone (#1433): keying the persistent shard by (sha, subdir)
        # ensures the cached working tree is the subdir only (<2 MB) instead of
        # the full repo. Bare cache is unchanged so variants share object data.
        return persistent_cache.get_checkout(
            canonical_url,
            resolved_sha or ref,
            locked_sha=resolved_sha,
            env=downloader._git_env_dict(),
            sparse_paths=[subdir_path],
        )
    except Exception:
        # Cache miss or failure -- fall through to normal clone path.
        return None


def _subdir_log_persistent_hit(perf_logger, dep_display, ref, subdir_path, checkout) -> None:
    """Emit the verbose [perf] lines for a persistent-cache hit."""
    from apm_cli.deps import github_downloader as _gh

    if perf_logger is None:
        return
    sha_short = (ref or "")[:12] if ref and re.match(r"^[a-f0-9]{7,40}$", ref) else ""
    perf_logger.subdir_download_start(
        dep_display,
        cache_state="persistent-hit",
        sha_short=sha_short,
        sparse_paths=[subdir_path],
    )
    perf_logger.materialize_result(
        sparse_applied=True,
        consumer_size_bytes=_gh._dir_size_bytes(checkout),
    )


def _subdir_shared_bare_materialize(
    downloader,
    dep_ref,
    ref,
    subdir_path,
    cache_host,
    cache_owner,
    cache_repo,
    shared_cache,
    perf_logger,
    dep_display,
    state,
) -> Path:
    """WS2: share a BARE clone keyed by (host, owner, repo, ref); materialize per consumer.

    The bare is subdir-agnostic, so concurrent consumers requesting different
    subdirectories of the same repo+ref share one bare without racing on
    sparse-checkout. Each consumer materializes its own working tree.
    """
    from apm_cli.deps import github_downloader as _gh

    from ..config import get_apm_temp_dir

    is_commit_sha = ref and re.match(r"^[a-f0-9]{7,40}$", ref) is not None
    perf_t0_bare = _gh.time.monotonic()

    def _shared_bare_clone_fn(bare_target: Path) -> None:
        downloader._bare_clone_with_fallback(
            dep_ref.repo_url,
            bare_target,
            dep_ref=dep_ref,
            ref=ref,
            is_commit_sha=bool(is_commit_sha),
        )

    def _shared_bare_fetch_fn(existing_bare: Path, ref_or_sha: str) -> bool:
        # get_or_clone passes `ref` here; for SHA pins it is the SHA.
        return downloader._fetch_sha_into_bare(existing_bare, ref_or_sha, dep_ref=dep_ref)

    try:
        shared_bare_path = shared_cache.get_or_clone(
            cache_host,
            cache_owner,
            cache_repo,
            ref,
            _shared_bare_clone_fn,
            fetch_fn=_shared_bare_fetch_fn if is_commit_sha else None,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to clone repository: {e}") from e
    perf_bare_elapsed_ms = int((_gh.time.monotonic() - perf_t0_bare) * 1000)
    if perf_logger is not None:
        strategy = (
            f"init+fetch --depth=1 origin {ref[:12]}"
            if is_commit_sha
            else f"--depth=1 --branch {ref or '<default>'}"
        )
        perf_logger.subdir_download_start(
            dep_display,
            cache_state="shared-bare",
            sha_short=ref[:12] if is_commit_sha and ref else "",
            sparse_paths=[subdir_path],
        )
        perf_logger.bare_clone_strategy(strategy, perf_bare_elapsed_ms)

    # Per-consumer materialization. mkdtemp gives a unique path so concurrent
    # consumers do not collide. The bare is read-only after this point.
    state.temp_dir = _gh.tempfile.mkdtemp(dir=get_apm_temp_dir())
    temp_clone_path = Path(state.temp_dir) / "consumer"
    try:
        state.ws2_resolved_commit = downloader._materialize_from_bare(
            shared_bare_path,
            temp_clone_path,
            ref=ref,
            env=downloader._git_env_dict(),
            # Only short-circuit SHA resolution for a full 40-char SHA;
            # abbreviated SHAs must be resolved against the bare so
            # resolved_commit matches head.commit.hexsha (#1135).
            known_sha=ref if (is_commit_sha and len(ref) == 40) else None,
            # Sparse-cone (#1433): materialize ONLY the subdirectory we need.
            sparse_paths=[subdir_path],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to prepare dependency from cached clone: {e}") from e
    if perf_logger is not None:
        perf_logger.materialize_result(
            sparse_applied=True,
            consumer_size_bytes=_gh._dir_size_bytes(temp_clone_path),
        )
    return temp_clone_path


def _subdir_legacy_clone(
    downloader, dep_ref, ref, subdir_path, progress_task_id, progress_obj, state
) -> Path:
    """Legacy per-dep clone path (no shared cache): sparse-checkout then full clone."""
    from apm_cli.deps import github_downloader as _gh

    from ..config import get_apm_temp_dir

    state.temp_dir = _gh.tempfile.mkdtemp(dir=get_apm_temp_dir())
    # Sparse checkout always targets "repo/". If it fails we clone into
    # "repo_clone/" so we never have to rmtree a directory that may still have
    # live git handles from the failed subprocess.
    sparse_clone_path = Path(state.temp_dir) / "repo"
    temp_clone_path = sparse_clone_path

    if progress_obj and progress_task_id is not None:
        progress_obj.update(progress_task_id, completed=20, total=100)

    # Phase 4 (#171): Try sparse-checkout first (git 2.25+), fall back to full clone.
    sparse_ok = downloader._try_sparse_checkout(dep_ref, sparse_clone_path, subdir_path, ref)
    if sparse_ok:
        return temp_clone_path

    # Full clone into a fresh subdirectory so we don't have to touch the
    # (possibly locked) sparse-checkout directory at all.
    temp_clone_path = Path(state.temp_dir) / "repo_clone"

    package_display_name = subdir_path.split("/")[-1]
    progress_reporter = (
        _gh.GitProgressReporter(progress_task_id, progress_obj, package_display_name)
        if progress_task_id and progress_obj
        else None
    )

    # Detect if ref is a commit SHA (can't be used with --branch in shallow clones).
    is_commit_sha = ref and re.match(r"^[a-f0-9]{7,40}$", ref) is not None

    clone_kwargs = {"dep_ref": dep_ref}
    if is_commit_sha:
        # For commit SHAs, clone without checkout then checkout the specific commit.
        clone_kwargs["no_checkout"] = True
    else:
        clone_kwargs["depth"] = 1
        if ref:
            clone_kwargs["branch"] = ref

    try:
        downloader._clone_with_fallback(
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
            repo_obj = _gh.Repo(temp_clone_path)
            repo_obj.git.checkout(ref)
        except Exception as e:
            raise RuntimeError(f"Failed to checkout commit {ref}: {e}") from e
        finally:
            _gh._close_repo(repo_obj)

    if progress_reporter:
        progress_reporter.disabled = True
    return temp_clone_path


def _subdir_extract_to_target(
    downloader, temp_clone_path, subdir_path, target_path, ws2_resolved_commit
) -> str:
    """Copy the subdirectory into target_path and resolve the commit SHA."""
    from apm_cli.deps import github_downloader as _gh

    from ..utils.file_ops import robust_copy2, robust_copytree
    from ..utils.path_security import ensure_path_within

    source_subdir = temp_clone_path / subdir_path
    # Security: ensure subdirectory resolves within the cloned repo.
    ensure_path_within(source_subdir, temp_clone_path)
    if not source_subdir.exists():
        raise RuntimeError(f"Subdirectory '{subdir_path}' not found in repository")
    if not source_subdir.is_dir():
        raise RuntimeError(f"Path '{subdir_path}' is not a directory")

    target_path.mkdir(parents=True, exist_ok=True)

    # If target exists and has content, remove it.
    if target_path.exists() and any(target_path.iterdir()):
        _gh._rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)

    for item in source_subdir.iterdir():
        src = source_subdir / item.name
        dst = target_path / item.name
        if src.is_dir():
            robust_copytree(src, dst)
        else:
            robust_copy2(src, dst)

    # Capture commit SHA; close the Repo immediately so its handles are released
    # before _rmtree runs. The WS2 path already resolved the SHA from the bare
    # (avoids opening Repo on the consumer dir, which leaks a Windows handle).
    if ws2_resolved_commit is not None:
        return ws2_resolved_commit
    repo = None
    try:
        repo = _gh.Repo(temp_clone_path)
        return repo.head.commit.hexsha
    except Exception:
        return "unknown"
    finally:
        _gh._close_repo(repo)


def _subdir_reraise_access_error(exc, temp_dir) -> None:
    """Translate a temp-dir permission error into an actionable RuntimeError."""
    exc_path = getattr(exc, "filename", None)
    # If temp_dir wasn't created (mkdtemp failed) or the error is within the
    # temp tree, this is likely a restricted temp directory issue.
    if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
        raise RuntimeError(
            "Access denied in temporary directory"
            + (f" '{temp_dir}'" if temp_dir else "")
            + ". Corporate security may restrict this path. "
            "Fix: apm config set temp-dir <WRITABLE_PATH>"
        ) from None


def _subdir_build_package_info(
    target_path, ref, resolved_commit, dep_ref, progress_task_id, progress_obj
) -> PackageInfo:
    """Validate the extracted package, stamp version, and build PackageInfo."""
    from apm_cli.deps import github_downloader as _gh

    from .package_validator import stamp_plugin_version

    validation_result = _gh.validate_apm_package(target_path)
    if not validation_result.is_valid:
        error_msgs = "; ".join(validation_result.errors)
        raise RuntimeError(f"Subdirectory is not a valid APM package or Claude Skill: {error_msgs}")

    resolved_ref = ResolvedReference(
        original_ref=ref or "default",
        ref_name=ref or "default",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit=resolved_commit,
    )

    # For plugins without an explicit version, stamp with the short commit SHA.
    package = validation_result.package
    stamp_plugin_version(
        package,
        validation_result.package_type,
        resolved_commit,
        target_path,
    )

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
