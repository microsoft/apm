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


@pytest.mark.parametrize("remote_suffix", ("", ".git"))
def test_install_url_rewrite_is_isolated_and_resolves_bare_and_git_forms(
    tmp_path: Path,
    remote_suffix: str,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
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
    remote_base = "https://github.example.invalid/acme/package"

    remote_forms = factory.install_url_rewrite(
        repository,
        f"{remote_base}{remote_suffix}",
    )
    assert factory.install_url_rewrite(repository, remote_base) == remote_forms

    assert remote_forms == (remote_base, f"{remote_base}.git")
    key = f"url.{repository.file_url}/.insteadOf"
    configured = _run_git(
        "config",
        "--file",
        environment["GIT_CONFIG_GLOBAL"],
        "--get-all",
        key,
        environment=environment,
    )
    assert configured.stdout.splitlines() == list(remote_forms)
    assert not (home / ".gitconfig").exists()

    for remote_form in remote_forms:
        resolved = _run_git(
            "ls-remote",
            remote_form,
            "refs/heads/main",
            environment=environment,
        )
        assert resolved.stdout.split()[0] == commit.sha

    adjacent = factory.create("package.git-fork")
    (adjacent.worktree / "apm.yml").write_text(
        "name: adjacent\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory.commit(adjacent, message="seed adjacent")
    adjacent_remote = _run_git(
        "ls-remote",
        f"{remote_base}.git-fork.git",
        "refs/heads/main",
        environment=environment,
        check=False,
    )
    assert adjacent_remote.returncode != 0


@pytest.mark.parametrize("remote_suffix", ("", ".git"))
def test_url_rewrite_subprocess_env_returns_fresh_full_env_for_both_forms(
    tmp_path: Path,
    remote_suffix: str,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
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
    remote_base = "https://github.example.invalid/acme/package"

    child_env = factory.url_rewrite_subprocess_env(
        repository,
        f"{remote_base}{remote_suffix}",
    )

    key = f"url.{repository.file_url}/.insteadOf"
    assert child_env["GIT_CONFIG_COUNT"] == "2"
    assert child_env["GIT_CONFIG_KEY_0"] == key
    assert child_env["GIT_CONFIG_KEY_1"] == key
    assert child_env["GIT_CONFIG_VALUE_0"] == remote_base
    assert child_env["GIT_CONFIG_VALUE_1"] == f"{remote_base}.git"
    # Full child env, not a caller-merged overlay: every fixture env entry
    # (e.g. the isolated GIT_CONFIG_GLOBAL) survives untouched alongside it.
    for name, value in environment.items():
        assert child_env[name] == value

    for remote_form in (remote_base, f"{remote_base}.git"):
        resolved = _run_git(
            "ls-remote",
            remote_form,
            "refs/heads/main",
            environment=child_env,
        )
        assert resolved.stdout.split()[0] == commit.sha


def test_url_rewrite_subprocess_env_does_not_mutate_input_or_factory_state(
    tmp_path: Path,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    original_environment = dict(environment)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
    )
    repository = factory.create("package")
    (repository.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory.commit(repository, message="seed")
    remote_base = "https://github.example.invalid/acme/package"

    first_env = factory.url_rewrite_subprocess_env(repository, remote_base)
    second_env = factory.url_rewrite_subprocess_env(repository, remote_base)

    # The mapping the caller constructed (and handed to the constructor)
    # must be untouched -- both by construction and by repeated calls.
    assert environment == original_environment
    assert "GIT_CONFIG_COUNT" not in environment
    # Each call returns a fresh, independent dict: equal contents, distinct
    # identity, and mutating one must never leak into the other or into a
    # subsequent call.
    assert first_env == second_env
    assert first_env is not second_env
    first_env["GIT_CONFIG_VALUE_0"] = "tampered"
    third_env = factory.url_rewrite_subprocess_env(repository, remote_base)
    assert third_env == second_env


def test_url_rewrite_subprocess_env_validates_ownership_and_remote_url_before_returning(
    tmp_path: Path,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
    )
    repository = factory.create("package")
    (repository.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory.commit(repository, message="seed")

    for remote_url in (
        "",
        " https://github.example.invalid/acme/package",
        "file:///tmp/package.git",
        "https://token@github.example.invalid/acme/package.git",
        "https://github.example.invalid/acme/package?ref=main",
        "https://github.example.invalid/acme/package#main",
        "https://github.example.invalid/acme/../package",
        "https://github.example.invalid/acme/package/",
    ):
        with pytest.raises(ValueError):
            factory.url_rewrite_subprocess_env(repository, remote_url)
    assert "GIT_CONFIG_COUNT" not in environment

    forged = LocalGitRepository(
        origin=repository.origin,
        worktree=repository.worktree,
    )
    with pytest.raises(ValueError, match="not owned"):
        factory.url_rewrite_subprocess_env(
            forged,
            "https://github.example.invalid/acme/package",
        )
    assert "GIT_CONFIG_COUNT" not in environment


def test_url_rewrite_subprocess_env_contains_collisions_to_owned_repository(
    tmp_path: Path,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
    )
    repository = factory.create("package")
    (repository.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory.commit(repository, message="seed")
    adjacent = factory.create("package.git-fork")
    (adjacent.worktree / "apm.yml").write_text(
        "name: adjacent\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory.commit(adjacent, message="seed adjacent")
    remote_base = "https://github.example.invalid/acme/package"

    child_env = factory.url_rewrite_subprocess_env(repository, remote_base)

    # Git's prefix-matching insteadOf rewrite must not let a request for an
    # unrelated, adjacently-named remote resolve through this repository's
    # rewrite base -- the trailing "/" containment must carry over from
    # install_url_rewrite.
    adjacent_remote = _run_git(
        "ls-remote",
        f"{remote_base}.git-fork.git",
        "refs/heads/main",
        environment=child_env,
        check=False,
    )
    assert adjacent_remote.returncode != 0


def test_url_rewrite_subprocess_env_rejects_preexisting_process_git_config(
    tmp_path: Path,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    remote_base = "https://github.example.invalid/acme/package"

    polluted_by_count = dict(environment)
    polluted_by_count["GIT_CONFIG_COUNT"] = "1"
    polluted_by_count["GIT_CONFIG_KEY_0"] = "credential.helper"
    polluted_by_count["GIT_CONFIG_VALUE_0"] = ""
    factory_with_count = LocalGitRepositoryFactory(
        tmp_path / "repositories-count",
        env=polluted_by_count,
    )
    repository_with_count = factory_with_count.create("package")
    (repository_with_count.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory_with_count.commit(repository_with_count, message="seed")
    with pytest.raises(ValueError, match="GIT_CONFIG_COUNT"):
        factory_with_count.url_rewrite_subprocess_env(repository_with_count, remote_base)

    malformed_slot = dict(environment)
    malformed_slot["GIT_CONFIG_KEY_0"] = "credential.helper"
    factory_with_slot = LocalGitRepositoryFactory(
        tmp_path / "repositories-slot",
        env=malformed_slot,
    )
    repository_with_slot = factory_with_slot.create("package")
    (repository_with_slot.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory_with_slot.commit(repository_with_slot, message="seed")
    with pytest.raises(ValueError, match="GIT_CONFIG_KEY_0"):
        factory_with_slot.url_rewrite_subprocess_env(repository_with_slot, remote_base)


def test_url_rewrite_subprocess_env_rejects_preexisting_process_git_config_case_insensitively(
    tmp_path: Path,
) -> None:
    # Windows environment variable names are case-insensitive: a fixture
    # carrying "git_config_count" or "Git_Config_Key_0" occupies the exact
    # same slot there as the canonical uppercase name, so the fail-closed
    # guard must normalize case before comparing -- otherwise a
    # differently-cased pre-existing entry would slip past detection and
    # end up silently clobbered (or duplicated with undefined precedence)
    # alongside the ones this method writes.
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    remote_base = "https://github.example.invalid/acme/package"

    polluted_by_lower_count = dict(environment)
    polluted_by_lower_count["git_config_count"] = "1"
    polluted_by_lower_count["Git_Config_Key_0"] = "credential.helper"
    polluted_by_lower_count["Git_Config_Value_0"] = ""
    factory_with_lower_count = LocalGitRepositoryFactory(
        tmp_path / "repositories-lower-count",
        env=polluted_by_lower_count,
    )
    repository_with_lower_count = factory_with_lower_count.create("package")
    (repository_with_lower_count.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory_with_lower_count.commit(repository_with_lower_count, message="seed")
    snapshot_before = dict(factory_with_lower_count._env)
    with pytest.raises(ValueError, match="git_config_count"):
        factory_with_lower_count.url_rewrite_subprocess_env(
            repository_with_lower_count, remote_base
        )
    assert factory_with_lower_count._env == snapshot_before

    mixed_case_slot = dict(environment)
    mixed_case_slot["gIt_ConFiG_key_0"] = "credential.helper"
    factory_with_mixed_slot = LocalGitRepositoryFactory(
        tmp_path / "repositories-mixed-slot",
        env=mixed_case_slot,
    )
    repository_with_mixed_slot = factory_with_mixed_slot.create("package")
    (repository_with_mixed_slot.worktree / "apm.yml").write_text(
        "name: package\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    factory_with_mixed_slot.commit(repository_with_mixed_slot, message="seed")
    snapshot_before_slot = dict(factory_with_mixed_slot._env)
    with pytest.raises(ValueError, match="gIt_ConFiG_key_0"):
        factory_with_mixed_slot.url_rewrite_subprocess_env(repository_with_mixed_slot, remote_base)
    assert factory_with_mixed_slot._env == snapshot_before_slot


def test_install_url_rewrite_rejects_unsafe_inputs_before_config_mutation(
    tmp_path: Path,
) -> None:
    environment = _git_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    factory = LocalGitRepositoryFactory(
        tmp_path / "repositories",
        env=environment,
    )
    repository = factory.create("package")
    global_config = Path(environment["GIT_CONFIG_GLOBAL"])
    original_global = global_config.read_bytes()

    for remote_url in (
        "",
        " https://github.example.invalid/acme/package",
        "file:///tmp/package.git",
        "https://token@github.example.invalid/acme/package.git",
        "https://github.example.invalid/acme/package?ref=main",
        "https://github.example.invalid/acme/package#main",
        "https://github.example.invalid/acme/../package",
        "https://github.example.invalid/acme/package/",
    ):
        with pytest.raises(ValueError):
            factory.install_url_rewrite(repository, remote_url)
    assert global_config.read_bytes() == original_global
    assert not (home / ".gitconfig").exists()

    forged = LocalGitRepository(
        origin=repository.origin,
        worktree=repository.worktree,
    )
    with pytest.raises(ValueError, match="not owned"):
        factory.install_url_rewrite(
            forged,
            "https://github.example.invalid/acme/package",
        )
    assert global_config.read_bytes() == original_global
    assert not (home / ".gitconfig").exists()

    outside_config = tmp_path / "outside.gitconfig"
    outside_config.write_bytes(b"[safe]\n\tvalue = unchanged\n")
    global_config.unlink()
    global_config.symlink_to(outside_config)
    with pytest.raises(ValueError, match="symlinked fixture Git config"):
        factory.install_url_rewrite(
            repository,
            "https://github.example.invalid/acme/package",
        )
    assert outside_config.read_bytes() == b"[safe]\n\tvalue = unchanged\n"
    assert not (home / ".gitconfig").exists()


def test_install_url_rewrite_requires_bounded_fixture_git_config(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario"
    scenario.mkdir()
    home = scenario / "home"
    home.mkdir()
    environment = _git_environment(tmp_path)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    outside_config = Path(environment["GIT_CONFIG_GLOBAL"])
    original_config = outside_config.read_bytes()
    factory = LocalGitRepositoryFactory(
        scenario / "repositories",
        env=environment,
    )
    repository = factory.create("package")

    with pytest.raises(ValueError, match="outside the allowed base"):
        factory.install_url_rewrite(
            repository,
            "https://github.example.invalid/acme/package",
        )
    assert outside_config.read_bytes() == original_config

    isolated_environment = _git_environment(scenario)
    isolated_environment.pop("GIT_CONFIG_NOSYSTEM")
    unguarded_factory = LocalGitRepositoryFactory(
        scenario / "unguarded-repositories",
        env=isolated_environment,
    )
    unguarded_repository = unguarded_factory.create("package")
    with pytest.raises(ValueError, match="GIT_CONFIG_NOSYSTEM=1"):
        unguarded_factory.install_url_rewrite(
            unguarded_repository,
            "https://github.example.invalid/acme/package",
        )


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
