"""Shared helper for sparse-checkout cone setup (perf #1433).

Extracted so the persistent git cache (``cache.git_cache``) and the
shared-bare materialization path (``deps.bare_cache``) configure
sparse-cone with identical subprocess semantics. Single place to evolve
sparse-checkout behavior (timeouts, additional flags, future
``--no-sparse-index``) without drift between the two call sites.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


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
