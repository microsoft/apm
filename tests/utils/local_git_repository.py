from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


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

    def __init__(self, root: Path, *, env: Mapping[str, str]) -> None:
        self._root = root
        self._env = dict(env)
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

        origin = self._root / f"{name}.git"
        worktree = self._root / f"{name}-worktree"
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
        return repository

    def commit(
        self,
        repository: LocalGitRepository,
        *,
        message: str,
    ) -> GitCommit:
        """Commit all working-tree changes and publish the main branch."""
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
        return GitCommit(sha=sha, message=message)

    def tag(
        self,
        repository: LocalGitRepository,
        name: str,
        target: GitCommit,
    ) -> None:
        """Create and publish a tag at the target commit."""
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
        return subprocess.run(
            command,
            cwd=cwd,
            env=self._env,
            capture_output=True,
            text=True,
            check=True,
        )
