"""Shared helper for sparse-checkout cone setup (perf #1433).

Extracted so the persistent git cache (``cache.git_cache``) and the
shared-bare materialization path (``deps.bare_cache``) configure
sparse-cone with identical subprocess semantics. Single place to evolve
sparse-checkout behavior (timeouts, additional flags, future
``--no-sparse-index``) without drift between the two call sites.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .path_security import PathTraversalError, ensure_path_within

FULL_CHECKOUT_TIMEOUT_SECONDS = 300


def _literal_pathspec(path: str) -> str:
    """Return a Git pathspec that treats every character in *path* literally."""
    return f":(literal){path}"


def _tracked_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> list[Path]:
    """Return materialized tracked symlinks under *paths*.

    Git's index identifies mode-120000 entries without walking every file
    in the cone. The filesystem check excludes platforms where Git writes
    symlink entries as plain files because ``core.symlinks`` is disabled.
    """
    if not paths:
        return []
    head = [git_exe, *(extra_git_args or [])]
    result = subprocess.run(
        [
            *head,
            "-C",
            str(repo_dir),
            "ls-tree",
            "-r",
            "-z",
            "HEAD",
            "--",
            *(_literal_pathspec(path) for path in paths),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=True,
    )
    symlinks: list[Path] = []
    for record in result.stdout.split("\0"):
        metadata, separator, relative = record.partition("\t")
        if separator and metadata.split(maxsplit=1)[0] == "120000":
            candidate = repo_dir / relative
            if os.path.islink(candidate):
                symlinks.append(candidate)
    return symlinks


def _first_dangling_tracked_symlink(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int,
    extra_git_args: list[str] | None,
) -> Path | None:
    """Validate tracked symlink containment and return the first broken link."""
    symlinks = _tracked_symlinks(
        git_exe,
        repo_dir,
        paths,
        env=env,
        timeout=timeout,
        extra_git_args=extra_git_args,
    )
    targets: list[tuple[Path, Path]] = []
    for link in symlinks:
        try:
            raw_target = Path(os.readlink(link))
        except OSError:
            if not os.path.exists(link):
                return link
            continue
        target = raw_target if raw_target.is_absolute() else link.parent / raw_target
        resolved_target = ensure_path_within(target, repo_dir)
        targets.append((link, resolved_target))

    if targets:
        head = [git_exe, *(extra_git_args or [])]
        relative_targets = [
            target.relative_to(repo_dir.resolve()).as_posix() for _, target in targets
        ]
        result = subprocess.run(
            [
                *head,
                "-C",
                str(repo_dir),
                "ls-tree",
                "-r",
                "-z",
                "HEAD",
                "--",
                *(_literal_pathspec(path) for path in relative_targets),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=True,
        )
        tracked_files = {
            relative
            for record in result.stdout.split("\0")
            if record
            for _, separator, relative in (record.partition("\t"),)
            if separator
        }
    else:
        tracked_files = set()

    for link, resolved_target in targets:
        relative_target = resolved_target.relative_to(repo_dir.resolve()).as_posix()
        if relative_target not in tracked_files:
            relative_link = link.relative_to(repo_dir)
            raise PathTraversalError(
                f"Symlink '{relative_link}' targets '{relative_target}', which is not "
                "a tracked file in the checked-out commit."
            )
        if not os.path.exists(link):
            return link
    return None


def validate_materialized_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
) -> None:
    """Reject broken or checkout-escaping symlinks before package copy."""
    dangling = _first_dangling_tracked_symlink(
        git_exe,
        repo_dir,
        paths,
        env=env,
        timeout=timeout,
        extra_git_args=extra_git_args,
    )
    if dangling is not None:
        relative = dangling.relative_to(repo_dir)
        raise RuntimeError(
            f"Symlink '{relative}' is unresolved after materializing the repository; "
            "repair its target in the package repository."
        )


def sparse_checkout_active(
    git_exe: str,
    repo_dir: Path,
    *,
    env: dict[str, str] | None,
    timeout: int = 10,
    extra_git_args: list[str] | None = None,
) -> bool:
    """Return whether Git still considers *repo_dir* a sparse checkout."""
    head = [git_exe, *(extra_git_args or [])]
    result = subprocess.run(
        [*head, "-C", str(repo_dir), "config", "--bool", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def repair_dangling_cone_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int = FULL_CHECKOUT_TIMEOUT_SECONDS,
    extra_git_args: list[str] | None = None,
    repair_env_factory: Callable[[], dict[str, str]] | None = None,
) -> Path | None:
    """Widen a cone checkout to a full tree if it left a dangling symlink.

    Call AFTER the cone checkout (``apply_sparse_cone`` + ``git
    checkout``) completes. Queries the Git index for tracked symlinks in
    the requested ``paths`` and checks whether a target was excluded by
    the cone. If one is found,
    falls back to ``git sparse-checkout disable`` so every symlink
    target that exists anywhere in the tree resolves (#2707). In a plain
    clone the full tree repopulates from objects already fetched; in a
    partial clone (``--filter=blob:none`` promisor remotes) the disable
    fetches the missing blobs from the remote at repair time.

    This trades the perf-#1433 disk savings for correctness on the repos
    that need it -- a dependency whose payload is mostly symlinks into
    the repo root loses the sparse win on every install. The common case
    (no cross-cone symlinks) checks only mode-120000 index entries and
    never disables sparse-checkout.

    ``repair_env_factory`` defers remote validation and credential setup until
    the uncommon widening path can fetch promised blobs.

    Returns:
        The first dangling symlink found (repo-relative resolution
        already applied by the caller's ``repo_dir``), or ``None`` if
        the cone had no dangling symlinks and no repair was needed.
    """
    dangling = _first_dangling_tracked_symlink(
        git_exe,
        repo_dir,
        paths,
        env=env,
        timeout=timeout,
        extra_git_args=extra_git_args,
    )
    if dangling is None:
        return None
    repair_env = repair_env_factory() if repair_env_factory is not None else env
    head = [git_exe, *(extra_git_args or [])]
    subprocess.run(
        [*head, "-C", str(repo_dir), "sparse-checkout", "disable"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=repair_env,
        check=True,
    )
    validate_materialized_symlinks(
        git_exe,
        repo_dir,
        paths,
        env=repair_env,
        timeout=timeout,
        extra_git_args=extra_git_args,
    )
    return dangling


def apply_sparse_cone(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
) -> None:
    """Initialize cone-mode sparse checkout and set the requested paths.

    Issues ``git sparse-checkout init --cone`` followed by
    ``git sparse-checkout set <paths...>`` inside ``repo_dir``. Both
    subprocesses run with ``check=True``; failures propagate to the
    caller so silent fallback to a full checkout (which would defeat
    the perf invariant from #1433) is impossible.

    Args:
        git_exe: Absolute path to the git executable.
        repo_dir: Repository working tree to configure.
        paths: Top-level cone paths to materialize. Must be non-empty.
        env: Subprocess environment (auth / safe.bareRepository etc.).
        timeout: Per-subprocess timeout in seconds.
        extra_git_args: Extra args inserted between the git executable
            and the first subcommand (e.g. ``["-c", "core.longpaths=true"]``
            on Windows so the long staged path under ``checkouts_v1/``
            does not trip MAX_PATH when git locks ``.git/config``).
    """
    if not paths:
        return
    head = [git_exe, *(extra_git_args or [])]
    subprocess.run(
        [*head, "-C", str(repo_dir), "sparse-checkout", "init", "--cone"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=True,
    )
    subprocess.run(
        [*head, "-C", str(repo_dir), "sparse-checkout", "set", *paths],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=True,
    )
