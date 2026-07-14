from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)

_GIT_TIMEOUT_SECONDS = 10.0


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
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        env=environment,
        capture_output=True,
        text=True,
        check=check,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _assert_commit_poison_is_active(
    root: Path,
    environment: dict[str, str],
) -> None:
    _run_git(
        "init",
        "--initial-branch=main",
        str(root),
        environment=environment,
    )
    (root / "payload.txt").write_text("poison control\n", encoding="utf-8")
    _run_git("-C", str(root), "add", "--all", environment=environment)
    commit = _run_git(
        "-C",
        str(root),
        "commit",
        "-m",
        "must fail",
        environment=environment,
        check=False,
    )
    assert commit.returncode != 0
    head = _run_git(
        "-C",
        str(root),
        "rev-parse",
        "--verify",
        "HEAD",
        environment=environment,
        check=False,
    )
    assert head.returncode != 0


def test_create_owns_bare_origin_and_isolated_worktree(tmp_path: Path) -> None:
    environment = _git_environment(tmp_path)
    source_tree = tmp_path / "source"
    source_tree.mkdir()
    manifest_bytes = b"name: package\nversion: 0.1.0\n"
    binary_bytes = b"\x00\xfflocal-git-fixture\n\x10"
    (source_tree / "apm.yml").write_bytes(manifest_bytes)
    nested = source_tree / "nested"
    nested.mkdir()
    (nested / "payload.bin").write_bytes(binary_bytes)
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
    assert (repository.worktree / "apm.yml").read_bytes() == manifest_bytes
    assert (repository.worktree / "nested" / "payload.bin").read_bytes() == binary_bytes
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
    origin_head = _run_git(
        "-C",
        str(repository.origin),
        "symbolic-ref",
        "HEAD",
        environment=environment,
    )
    assert origin_head.stdout.strip() == "refs/heads/main"
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
    current_branch = _run_git(
        "-C",
        str(repository.worktree),
        "branch",
        "--show-current",
        environment=environment,
    )
    assert current_branch.stdout.strip() == "main"

    for unsafe_name in (
        ".",
        "..",
        "../outside",
        "nested/repository",
        r"nested\repository",
        "Z:outside",
    ):
        with pytest.raises(ValueError, match="Unsafe repository name"):
            factory.create(unsafe_name)

    assert not (tmp_path / "outside.git").exists()
    assert not (tmp_path / "outside-worktree").exists()
    with pytest.raises(FileExistsError, match="origin already exists"):
        factory.create("package")


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


def test_same_source_sequence_produces_deterministic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned_config = tmp_path / "poisoned.gitconfig"
    poisoned_config.write_text(
        "[commit]\n"
        "    gpgsign = true\n"
        "[gpg]\n"
        "    program = /definitely/missing/apm-test-gpg\n"
        "[user]\n"
        "    name = Poisoned Config\n"
        "    email = poisoned-config@example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Poisoned Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "poisoned-author@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Poisoned Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "poisoned-committer@example.invalid")

    commits = []
    for name, poison_source in (("first", "global"), ("second", "counted")):
        if poison_source == "global":
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(poisoned_config))
            monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
            monkeypatch.delenv("GIT_CONFIG_KEY_0", raising=False)
            monkeypatch.delenv("GIT_CONFIG_VALUE_0", raising=False)
            monkeypatch.delenv("GIT_CONFIG_KEY_1", raising=False)
            monkeypatch.delenv("GIT_CONFIG_VALUE_1", raising=False)
        else:
            monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
            monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
            monkeypatch.setenv("GIT_CONFIG_KEY_1", "gpg.program")
            monkeypatch.setenv(
                "GIT_CONFIG_VALUE_1",
                "/definitely/missing/apm-test-gpg",
            )

        _assert_commit_poison_is_active(
            tmp_path / f"{name}-poison-control",
            dict(os.environ),
        )
        scenario = tmp_path / name
        scenario.mkdir()
        environment = _git_environment(scenario)
        factory = LocalGitRepositoryFactory(
            scenario / "repositories",
            env=environment,
        )
        repository = factory.create("package")
        (repository.worktree / "apm.yml").write_text(
            "name: package\nversion: 0.1.0\n",
            encoding="utf-8",
        )
        commits.append(factory.commit(repository, message="seed"))
        identity = _run_git(
            "-C",
            str(repository.worktree),
            "show",
            "-s",
            "--format=%an%n%ae%n%cn%n%ce",
            "HEAD",
            environment=environment,
        )
        assert identity.stdout.splitlines() == [
            "APM Test",
            "apm-test@example.invalid",
            "APM Test",
            "apm-test@example.invalid",
        ]
        configured_user_name = _run_git(
            "-C",
            str(repository.worktree),
            "config",
            "--get",
            "user.name",
            environment=environment,
            check=False,
        )
        assert configured_user_name.returncode == 1
        assert configured_user_name.stdout == ""

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
    manifest.write_text("name: package\nversion: 0.2.0\n", encoding="utf-8")
    second = factory.commit(repository, message="second")
    head_at_tag_creation = _run_git(
        "-C",
        str(repository.worktree),
        "rev-parse",
        "HEAD",
        environment=environment,
    )
    assert head_at_tag_creation.stdout.strip() == second.sha
    assert first.sha != second.sha
    factory.tag(repository, "v1.0.0", first)

    local_initial_tag = _run_git(
        "-C",
        str(repository.worktree),
        "rev-parse",
        "refs/tags/v1.0.0",
        environment=environment,
    )
    assert local_initial_tag.stdout.strip() == first.sha
    initial_tag = _run_git(
        "ls-remote",
        repository.file_url,
        "refs/tags/v1.0.0",
        environment=environment,
    )
    assert initial_tag.stdout.split()[0] == first.sha

    manifest.write_text("name: package\nversion: 0.3.0\n", encoding="utf-8")
    third = factory.commit(repository, message="third")
    assert second.sha != third.sha
    head_at_tag_advance = _run_git(
        "-C",
        str(repository.worktree),
        "rev-parse",
        "HEAD",
        environment=environment,
    )
    assert head_at_tag_advance.stdout.strip() == third.sha
    factory.advance_tag(repository, "v1.0.0", second)

    local_advanced_tag = _run_git(
        "-C",
        str(repository.worktree),
        "rev-parse",
        "refs/tags/v1.0.0",
        environment=environment,
    )
    assert local_advanced_tag.stdout.strip() == second.sha
    advanced_tag = _run_git(
        "ls-remote",
        repository.file_url,
        "refs/tags/v1.0.0",
        environment=environment,
    )
    assert advanced_tag.stdout.split()[0] == second.sha


def test_repository_and_commit_records_are_immutable_and_factory_owned(
    tmp_path: Path,
) -> None:
    environment = _git_environment(tmp_path)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
    )
    repository = factory.create("package")
    (repository.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    commit = factory.commit(repository, message="seed")

    with pytest.raises(FrozenInstanceError):
        repository.origin = tmp_path / "retargeted.git"
    with pytest.raises(FrozenInstanceError):
        commit.sha = "0" * 40

    forged_repository = LocalGitRepository(
        origin=repository.origin,
        worktree=repository.worktree,
    )
    with pytest.raises(ValueError, match="not owned"):
        factory.commit(forged_repository, message="forged")
    forged_commit = GitCommit(sha=commit.sha, message=commit.message)
    with pytest.raises(ValueError, match="not owned"):
        factory.tag(repository, "forged", forged_commit)

    foreign_factory = LocalGitRepositoryFactory(
        tmp_path / "foreign-repositories",
        env=environment,
    )
    foreign_repository = foreign_factory.create("foreign")
    with pytest.raises(ValueError, match="not owned"):
        factory.commit(foreign_repository, message="foreign")

    outside = tmp_path / "outside-worktree"
    outside.mkdir()
    shutil.rmtree(repository.worktree)
    repository.worktree.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match=r"outside|symlinked repository path"):
        factory.commit(repository, message="escaped")


def test_create_rejects_preexisting_and_symlinked_paths(tmp_path: Path) -> None:
    environment = _git_environment(tmp_path)
    repositories = tmp_path / "repositories"
    repositories.mkdir()
    factory = LocalGitRepositoryFactory(repositories, env=environment)

    (repositories / "existing-worktree").mkdir()
    with pytest.raises(FileExistsError, match="worktree already exists"):
        factory.create("existing")
    assert not (repositories / "existing.git").exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked repository factory root"):
        LocalGitRepositoryFactory(linked_root, env=environment)

    (repositories / "linked.git").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError, match="origin already exists"):
        factory.create("linked")
    assert not (repositories / "linked-worktree").exists()

    source_tree = tmp_path / "source"
    source_tree.mkdir()
    (source_tree / "apm.yml").write_text("name: linked-source\n", encoding="utf-8")
    (source_tree / "escaped").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked source tree"):
        factory.create("linked-source", source_tree=source_tree)
    assert not (repositories / "linked-source.git").exists()
    assert not (repositories / "linked-source-worktree").exists()


def test_factory_passes_bounded_timeout_to_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _git_environment(tmp_path)
    timeout_seconds = 0.5
    observed_timeouts: list[float | None] = []

    def timeout_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        observed_timeouts.append(timeout if isinstance(timeout, float) else None)
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    monkeypatch.setattr(
        "tests.utils.local_git_repository.subprocess.run",
        timeout_run,
    )
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
        timeout_seconds=timeout_seconds,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        factory.create("timeout")
    assert observed_timeouts == [timeout_seconds]


def test_factory_honors_shared_scenario_deadline(tmp_path: Path) -> None:
    environment = _git_environment(tmp_path)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
        timeout_seconds=30,
        deadline=time.monotonic() - 1,
    )

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        factory.create("expired")

    assert exc_info.value.cmd[:3] == (
        "git",
        "init",
        "--bare",
    )
    assert exc_info.value.timeout == 0
