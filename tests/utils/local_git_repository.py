from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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

    def install_url_rewrite(
        self,
        repository: LocalGitRepository,
        remote_url: str,
    ) -> tuple[str, str]:
        """Route bare and ``.git`` HTTP(S) forms to an owned local repository.

        Rewrites are written only to the explicit fixture global config. The
        local replacement ends in ``/`` so Git's prefix matching cannot append
        an adjacent remote suffix onto a sibling fixture repository path.
        """
        repository = self._owned_repository(repository)
        remote_forms = self._remote_url_forms(remote_url)
        config_path = self._fixture_git_config_path()
        rewrite_base = f"{repository.file_url}/"
        key = f"url.{rewrite_base}.insteadOf"
        configured = self._run(
            (
                "git",
                "config",
                "--file",
                str(config_path),
                "--get-all",
                key,
            ),
            cwd=self._root,
            check=False,
        )
        # Fixture setup is single-writer; serial dedup keeps repeated calls idempotent.
        existing = set(configured.stdout.splitlines())
        for remote_form in remote_forms:
            if remote_form in existing:
                continue
            self._run(
                (
                    "git",
                    "config",
                    "--file",
                    str(config_path),
                    "--add",
                    key,
                    remote_form,
                ),
                cwd=self._root,
            )
        return remote_forms

    def url_rewrite_subprocess_env(
        self,
        repository: LocalGitRepository,
        remote_url: str,
    ) -> dict[str, str]:
        """Return a fresh child env routing an HTTP(S) URL to an owned local repository.

        Unlike :meth:`install_url_rewrite`, which writes the rewrite to the
        fixture's explicit ``GIT_CONFIG_GLOBAL`` file, this returns a full
        environment dict carrying the rewrite as process-scoped
        ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_`` / ``GIT_CONFIG_VALUE_``
        entries. Production ``GitAuthEnvBuilder.setup_environment`` replaces
        ``GIT_CONFIG_GLOBAL`` with ``/dev/null`` for auth-bearing clone
        operations, which silently defeats the global-config fixture above;
        it does not touch indexed ``GIT_CONFIG_*`` process config, so a
        caller that runs a real subprocess (e.g. the CLI) with the returned
        env keeps the rewrite intact through that auth path.

        Returns a brand-new dict derived from this factory's isolated env --
        never a partial overlay the caller must merge themselves, and never
        a mutation of the factory's own env or the mapping passed to it.
        The local replacement ends in ``/`` so Git's prefix matching cannot
        append an adjacent remote suffix onto a sibling fixture repository
        path (same containment as :meth:`install_url_rewrite`).
        """
        repository = self._owned_repository(repository)
        remote_forms = self._remote_url_forms(remote_url)
        self._reject_preexisting_process_git_config(self._env)
        rewrite_base = f"{repository.file_url}/"
        key = f"url.{rewrite_base}.insteadOf"
        env = dict(self._env)
        env["GIT_CONFIG_COUNT"] = str(len(remote_forms))
        for index, remote_form in enumerate(remote_forms):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = remote_form
        return env

    @staticmethod
    def _reject_preexisting_process_git_config(env: Mapping[str, str]) -> None:
        """Fail closed rather than silently clobber inherited process config.

        Windows environment variable names are case-insensitive, so
        ``git_config_count`` or ``Git_Config_Key_0`` are the *same* slot as
        their uppercase forms there -- comparisons must normalize case
        before matching, or a differently-cased pre-existing entry would
        silently coexist with (and be clobbered by) the ones this method
        writes. Names are upper-cased only for the equality/prefix check;
        the original (as-provided) names are reported in errors.
        """
        count_names = sorted(name for name in env if name.upper() == "GIT_CONFIG_COUNT")
        if count_names:
            raise ValueError(
                "Fixture env already declares a process-scoped Git config count "
                f"under {', '.join(count_names)!r}; url_rewrite_subprocess_env "
                "requires a clean process-config slate"
            )
        stray = sorted(
            name
            for name in env
            if name.upper().startswith("GIT_CONFIG_KEY_")
            or name.upper().startswith("GIT_CONFIG_VALUE_")
        )
        if stray:
            raise ValueError(
                "Fixture env already declares indexed Git config overrides: " + ", ".join(stray)
            )
    def _run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool = True,
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
            check=check,
            timeout=timeout_seconds,
        )

    @staticmethod
    def _remote_url_forms(remote_url: str) -> tuple[str, str]:
        if not remote_url or remote_url.strip() != remote_url:
            raise ValueError("Remote URL must be a non-empty string without surrounding whitespace")
        parsed = urlsplit(remote_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Remote URL must use an HTTP(S) production host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Remote URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Remote URL must not contain a query or fragment")
        if parsed.path.endswith("/"):
            raise ValueError("Remote URL must identify a repository, not a directory")
        path = parsed.path.lstrip("/")
        validate_path_segments(path, context="remote repository URL", reject_empty=True)
        if len(path.split("/")) < 2:
            raise ValueError("Remote URL must include an owner and repository path")
        bare = remote_url.removesuffix(".git")
        if bare.endswith("/"):
            raise ValueError("Remote URL must include a repository name before .git")
        return bare, f"{bare}.git"

    def _fixture_git_config_path(self) -> Path:
        """Return the explicit config after anchoring it to fixture ``HOME``."""
        if self._env.get("GIT_CONFIG_NOSYSTEM") != "1":
            raise ValueError("Fixture Git rewrites require GIT_CONFIG_NOSYSTEM=1")
        raw_global = self._env.get("GIT_CONFIG_GLOBAL")
        raw_home = self._env.get("HOME")
        if not raw_global or not raw_home:
            raise ValueError("Fixture Git rewrites require explicit GIT_CONFIG_GLOBAL and HOME")
        global_config = Path(raw_global)
        home = Path(raw_home)
        if not global_config.is_absolute() or not home.is_absolute():
            raise ValueError("Fixture Git config and HOME paths must be absolute")
        if home.is_symlink() or not home.is_dir():
            raise ValueError(f"Fixture HOME must be a non-symlink directory: {home}")
        fixture_root = home.parent
        ensure_path_within(self._root, fixture_root)
        if global_config.is_symlink():
            raise ValueError(f"Refusing symlinked fixture Git config: {global_config}")
        ensure_path_within(global_config, fixture_root)
        if not global_config.parent.is_dir():
            raise ValueError(
                f"Fixture Git config parent must be a directory: {global_config.parent}"
            )
        if global_config.exists() and not global_config.is_file():
            raise ValueError(f"Fixture Git config must be a regular file: {global_config}")
        return global_config

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
