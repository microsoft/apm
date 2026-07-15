from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within, validate_path_segments


@dataclass(frozen=True)
class GitCommit:
    """A commit created by a local Git repository fixture."""

    sha: str
    message: str


@dataclass(frozen=True)
class LocalGitRepository:
    """A real local bare origin and its isolated working repository."""

    origin: Path
    worktree: Path

    @property
    def file_url(self) -> str:
        """Return the origin as a valid local Git remote URL."""
        return self.origin.resolve().as_uri()


class LocalGitRepositoryFactory:
    """Create and advance deterministic local Git repository fixtures."""

    def __init__(
        self,
        root: Path,
        *,
        env: Mapping[str, str],
        timeout_seconds: float = 30.0,
        deadline: float | None = None,
    ) -> None:
        if root.is_symlink():
            raise ValueError(f"Refusing symlinked repository factory root: {root}")
        self._root = root.resolve()
        self._env = dict(env)
        self._timeout_seconds = timeout_seconds
        self._deadline = deadline
        self._repositories: dict[int, LocalGitRepository] = {}
        self._commits: dict[int, GitCommit] = {}
        self._root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        *,
        source_tree: Path | None = None,
    ) -> LocalGitRepository:
        """Create a bare origin and working repository under the factory root."""
        if not name or "/" in name or "\\" in name or ":" in name or name in {".", ".."}:
            raise ValueError(f"Unsafe repository name: {name!r}")
        validate_path_segments(name, context="repository name", reject_empty=True)

        origin = self._root / f"{name}.git"
        worktree = self._root / f"{name}-worktree"
        if origin.exists() or origin.is_symlink():
            raise FileExistsError(f"Repository origin already exists: {origin}")
        if worktree.exists() or worktree.is_symlink():
            raise FileExistsError(f"Repository worktree already exists: {worktree}")
        ensure_path_within(origin, self._root)
        ensure_path_within(worktree, self._root)
        if source_tree is not None:
            self._validate_source_tree(source_tree)
        self._run(
            ("git", "init", "--bare", "--initial-branch=main", str(origin)),
            cwd=self._root,
        )
        self._run(
            ("git", "init", "--initial-branch=main", str(worktree)),
            cwd=self._root,
        )
        repository = LocalGitRepository(origin=origin, worktree=worktree)
        self._run(
            ("git", "remote", "add", "origin", repository.file_url),
            cwd=worktree,
        )
        if source_tree is not None:
            shutil.copytree(
                source_tree,
                worktree,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git"),
            )
        self._repositories[id(repository)] = repository
        return repository

    def commit(
        self,
        repository: LocalGitRepository,
        *,
        message: str,
    ) -> GitCommit:
        """Commit all working-tree changes and publish the main branch."""
        repository = self._owned_repository(repository)
        self._run(("git", "add", "--all"), cwd=repository.worktree)
        self._run(("git", "commit", "-m", message), cwd=repository.worktree)
        sha = self._run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository.worktree,
        ).stdout.strip()
        self._run(
            ("git", "push", "origin", "HEAD:refs/heads/main"),
            cwd=repository.worktree,
        )
        commit = GitCommit(sha=sha, message=message)
        self._commits[id(commit)] = commit
        return commit

    def tag(
        self,
        repository: LocalGitRepository,
        name: str,
        target: GitCommit,
    ) -> None:
        """Create and publish a tag at the target commit."""
        repository = self._owned_repository(repository)
        target = self._owned_commit(target)
        self._run(
            ("git", "tag", name, target.sha),
            cwd=repository.worktree,
        )
        self._run(
            ("git", "push", "origin", f"refs/tags/{name}"),
            cwd=repository.worktree,
        )

    def advance_tag(
        self,
        repository: LocalGitRepository,
        name: str,
        target: GitCommit,
    ) -> None:
        """Force an existing local and remote tag to the target commit."""
        repository = self._owned_repository(repository)
        target = self._owned_commit(target)
        self._run(
            ("git", "tag", "--force", name, target.sha),
            cwd=repository.worktree,
        )
        self._run(
            ("git", "push", "--force", "origin", f"refs/tags/{name}"),
            cwd=repository.worktree,
        )

    def _run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        timeout_seconds = self._timeout_seconds
        if self._deadline is not None:
            remaining_seconds = self._deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            timeout_seconds = min(timeout_seconds, remaining_seconds)
        return subprocess.run(
            command,
            cwd=cwd,
            env=self._env,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_seconds,
        )

    def _owned_repository(self, repository: LocalGitRepository) -> LocalGitRepository:
        if self._repositories.get(id(repository)) is not repository:
            raise ValueError("Local Git repository is not owned by this factory")
        ensure_path_within(repository.origin, self._root)
        ensure_path_within(repository.worktree, self._root)
        self._reject_symlink_components(repository.origin)
        self._reject_symlink_components(repository.worktree)
        return repository

    def _owned_commit(self, commit: GitCommit) -> GitCommit:
        if self._commits.get(id(commit)) is not commit:
            raise ValueError("Git commit is not owned by this factory")
        return commit

    def _validate_source_tree(self, source_tree: Path) -> None:
        if source_tree.is_symlink() or not source_tree.is_dir():
            raise ValueError(f"Source tree must be a non-symlink directory: {source_tree}")
        for source_path in source_tree.rglob("*"):
            if source_path.is_symlink():
                raise ValueError(f"Refusing symlinked source tree path: {source_path}")

    def _reject_symlink_components(self, path: Path) -> None:
        relative = path.relative_to(self._root)
        current = self._root
        if current.is_symlink():
            raise ValueError(f"Refusing symlinked repository path: {current}")
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"Refusing symlinked repository path: {current}")
