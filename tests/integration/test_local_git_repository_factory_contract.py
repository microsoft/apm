from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.utils.local_git_repository import LocalGitRepositoryFactory


def _git_environment(tmp_path: Path) -> dict[str, str]:
    git_config = tmp_path / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_AUTHOR_NAME": "APM Test",
        "GIT_AUTHOR_EMAIL": "apm-test@example.invalid",
        "GIT_COMMITTER_NAME": "APM Test",
        "GIT_COMMITTER_EMAIL": "apm-test@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }


def _run_git(
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def test_create_owns_bare_origin_and_isolated_worktree(tmp_path: Path) -> None:
    environment = _git_environment(tmp_path)
    source_tree = tmp_path / "source"
    source_tree.mkdir()
    (source_tree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    source_metadata = source_tree / ".git"
    source_metadata.mkdir()
    (source_metadata / "config").write_text("not real metadata", encoding="utf-8")
    repositories = tmp_path / "repositories"
    factory = LocalGitRepositoryFactory(repositories, env=environment)

    repository = factory.create("package", source_tree=source_tree)

    assert repository.origin != repository.worktree
    assert repository.origin.is_dir()
    assert repository.worktree.is_dir()
    assert repository.file_url == repository.origin.resolve().as_uri()
    assert (repository.worktree / "apm.yml").read_text(encoding="utf-8") == (
        "name: package\nversion: 0.1.0\n"
    )
    assert (repository.worktree / ".git" / "config").read_text(
        encoding="utf-8"
    ) != "not real metadata"
    bare = _run_git(
        "-C",
        str(repository.origin),
        "rev-parse",
        "--is-bare-repository",
        environment=environment,
    )
    assert bare.stdout.strip() == "true"
    remote = _run_git(
        "-C",
        str(repository.worktree),
        "remote",
        "get-url",
        "origin",
        environment=environment,
    )
    assert remote.stdout.strip() == repository.file_url
    worktree = _run_git(
        "-C",
        str(repository.worktree),
        "rev-parse",
        "--is-inside-work-tree",
        environment=environment,
    )
    assert worktree.stdout.strip() == "true"

    for unsafe_name in ("../outside", "nested/repository", r"nested\repository", "Z:outside"):
        with pytest.raises(ValueError, match="Unsafe repository name"):
            factory.create(unsafe_name)

    assert not (tmp_path / "outside.git").exists()
    assert not (tmp_path / "outside-worktree").exists()


def test_relative_root_resolves_once_at_the_intended_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _git_environment(tmp_path)
    monkeypatch.chdir(tmp_path)
    factory = LocalGitRepositoryFactory(Path("repositories"), env=environment)

    repository = factory.create("package")
    manifest = repository.worktree / "apm.yml"
    manifest.write_text("name: package\nversion: 0.1.0\n", encoding="utf-8")
    commit = factory.commit(repository, message="seed")

    expected_root = tmp_path / "repositories"
    assert repository.origin == expected_root / "package.git"
    assert repository.worktree == expected_root / "package-worktree"
    assert repository.file_url == (expected_root / "package.git").as_uri()
    assert not (expected_root / "repositories").exists()
    bare = _run_git(
        "-C",
        str(repository.origin),
        "rev-parse",
        "--is-bare-repository",
        environment=environment,
    )
    assert bare.stdout.strip() == "true"
    worktree = _run_git(
        "-C",
        str(repository.worktree),
        "rev-parse",
        "--is-inside-work-tree",
        environment=environment,
    )
    assert worktree.stdout.strip() == "true"
    remote_main = _run_git(
        "ls-remote",
        repository.file_url,
        "refs/heads/main",
        environment=environment,
    )
    assert remote_main.stdout.split()[0] == commit.sha


def test_same_source_sequence_produces_deterministic_commit(tmp_path: Path) -> None:
    commits = []
    for name in ("first", "second"):
        scenario = tmp_path / name
        scenario.mkdir()
        factory = LocalGitRepositoryFactory(
            scenario / "repositories",
            env=_git_environment(scenario),
        )
        repository = factory.create("package")
        (repository.worktree / "apm.yml").write_text(
            "name: package\nversion: 0.1.0\n",
            encoding="utf-8",
        )
        commits.append(factory.commit(repository, message="seed"))

    assert commits[0].sha == commits[1].sha
    assert commits[0].message == commits[1].message == "seed"


def test_tag_advancement_is_explicit_and_observable(tmp_path: Path) -> None:
    environment = _git_environment(tmp_path)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
    )
    repository = factory.create("package")
    manifest = repository.worktree / "apm.yml"
    manifest.write_text("name: package\nversion: 0.1.0\n", encoding="utf-8")
    first = factory.commit(repository, message="first")
    remote_main = _run_git(
        "ls-remote",
        repository.file_url,
        "refs/heads/main",
        environment=environment,
    )
    assert remote_main.stdout.split()[0] == first.sha
    factory.tag(repository, "v1.0.0", first)

    initial_tag = _run_git(
        "ls-remote",
        repository.file_url,
        "refs/tags/v1.0.0",
        environment=environment,
    )
    assert initial_tag.stdout.split()[0] == first.sha

    manifest.write_text("name: package\nversion: 0.2.0\n", encoding="utf-8")
    second = factory.commit(repository, message="second")
    factory.advance_tag(repository, "v1.0.0", second)

    advanced_tag = _run_git(
        "ls-remote",
        repository.file_url,
        "refs/tags/v1.0.0",
        environment=environment,
    )
    assert advanced_tag.stdout.split()[0] == second.sha
