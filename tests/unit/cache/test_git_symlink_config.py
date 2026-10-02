"""Real Git regression checks for checkout capability and config precedence."""

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils import git_env

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


def _git(env: dict[str, str], *args: str, input: str | None = None) -> str:
    """Run a bounded local Git command in the isolated test environment."""
    return subprocess.run(
        [git_env.get_git_executable(), *args],
        env=env,
        input=input,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Exclude ambient Git state without changing any real user config."""
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    for scope in ("GLOBAL", "SYSTEM"):
        config = tmp_path / f"{scope.lower()}.gitconfig"
        config.touch()
        monkeypatch.setenv(f"GIT_CONFIG_{scope}", str(config))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Fixture")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.test")
    return dict(os.environ)


@pytest.mark.parametrize("inherited_scope", ["system", "global"])
@pytest.mark.parametrize(
    ("inherited", "local_scope", "local", "command_source", "command", "expected"),
    [
        ("true", "local", "false", None, None, "false"),
        ("false", "local", "true", None, None, "true"),
        ("true", "worktree", "false", None, None, "false"),
        ("true", "local", "false", "indexed", "true", "true"),
        ("true", "local", "false", "parameters", "true", "true"),
        ("false", "worktree", "true", "indexed", "false", "false"),
        ("true", "local", None, None, None, "true"),
        ("false", "local", None, None, None, "false"),
    ],
)
def test_network_env_preserves_effective_symlink_setting(
    tmp_path: Path,
    config_env: dict[str, str],
    inherited_scope: str,
    inherited: str,
    local_scope: str,
    local: str | None,
    command_source: str | None,
    command: str | None,
    expected: str,
) -> None:
    """A capability result beats inherited settings, but not command intent."""
    config = config_env[f"GIT_CONFIG_{inherited_scope.upper()}"]
    _git(config_env, "config", "--file", config, "core.symlinks", inherited)
    _git(config_env, "config", "--file", config, "core.compression", "3")
    original_config = Path(config).read_bytes()
    repo = tmp_path / "repo"
    _git(config_env, "init", "--quiet", "--template=", str(repo))
    # Normalize the native init outcome so every precedence case runs on every OS.
    _git(config_env, "-C", str(repo), "config", "core.symlinks", "true")
    _git(config_env, "-C", str(repo), "config", "--unset", "core.symlinks")
    if local_scope == "worktree":
        _git(config_env, "-C", str(repo), "config", "extensions.worktreeConfig", "true")
    if local is not None:
        _git(config_env, "-C", str(repo), "config", f"--{local_scope}", "core.symlinks", local)
    if command_source == "indexed":
        assert command is not None
        config_env.update(
            GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.symlinks", GIT_CONFIG_VALUE_0=command
        )
    elif command_source == "parameters":
        config_env["GIT_CONFIG_PARAMETERS"] = f"'core.symlinks={command}'"
    query = ("-C", str(repo), "config", "--bool", "--get", "core.symlinks")
    assert _git(config_env, *query) == expected

    child = git_env.git_network_env("https://example.test/org/repo.git", config_env, worktree=repo)

    assert _git(child, *query) == expected
    assert _git(child, "-C", str(repo), "config", "--get", "core.compression") == "3"
    assert child["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child["GIT_CONFIG_SYSTEM"] == os.devnull
    assert Path(config).read_bytes() == original_config


@pytest.mark.parametrize(
    "sequence",
    [
        ("true", "false", "true"),
        ("false", "true", "false"),
        ("true", "false"),
        ("false", "true"),
        ("true", "true", "false"),
    ],
)
def test_network_env_preserves_last_repeated_command_value(
    tmp_path: Path,
    config_env: dict[str, str],
    sequence: tuple[str, ...],
) -> None:
    """Repeated indexed command-scope values must resolve to the last one.

    Guards against collapsing a repeated `GIT_CONFIG_KEY_N`/`VALUE_N` sequence
    (e.g. true, false, true) to the first occurrence instead of matching real
    Git's last-value-wins semantics for a single-valued setting.
    """
    repo = tmp_path / "repo"
    _git(config_env, "init", "--quiet", "--template=", str(repo))
    config_env["GIT_CONFIG_COUNT"] = str(len(sequence))
    for index, value in enumerate(sequence):
        config_env[f"GIT_CONFIG_KEY_{index}"] = "core.symlinks"
        config_env[f"GIT_CONFIG_VALUE_{index}"] = value
    query = ("-C", str(repo), "config", "--bool", "--get", "core.symlinks")
    expected = sequence[-1]
    assert _git(config_env, *query) == expected

    child = git_env.git_network_env("https://example.test/org/repo.git", config_env, worktree=repo)

    assert _git(child, *query) == expected


def test_network_env_symlink_precedence_preserves_auth_isolation(
    tmp_path: Path,
    config_env: dict[str, str],
) -> None:
    """Symlink precedence resolution must not resurrect scrubbed auth state.

    Regression-traps the shared entry loop in
    ``_materialize_git_config_snapshot``: a repeated command-scope
    ``core.symlinks`` sequence is interleaved with a command-scope
    ``credential.helper`` reset and a stale ``http.extraheader``. A bug that
    over-broadly widened the symlinks continue/retention branch could just as
    easily let the fenced auth entries pass through unfiltered.
    """
    repo = tmp_path / "repo"
    _git(config_env, "init", "--quiet", "--template=", str(repo))
    config_env["GIT_CONFIG_COUNT"] = "5"
    config_env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    config_env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic stale"
    config_env["GIT_CONFIG_KEY_1"] = "core.symlinks"
    config_env["GIT_CONFIG_VALUE_1"] = "true"
    config_env["GIT_CONFIG_KEY_2"] = "credential.helper"
    config_env["GIT_CONFIG_VALUE_2"] = ""
    config_env["GIT_CONFIG_KEY_3"] = "core.symlinks"
    config_env["GIT_CONFIG_VALUE_3"] = "false"
    config_env["GIT_CONFIG_KEY_4"] = "credential.helper"
    config_env["GIT_CONFIG_VALUE_4"] = "!stale-helper"

    child = git_env.git_network_env("https://example.test/org/repo.git", config_env, worktree=repo)

    query = ("-C", str(repo), "config", "--bool", "--get", "core.symlinks")
    assert _git(child, *query) == "false"

    assert "GIT_HTTP_EXTRAHEADER" not in child
    entries = {
        (
            child.get(f"GIT_CONFIG_KEY_{index}", ""),
            child.get(f"GIT_CONFIG_VALUE_{index}", ""),
        )
        for index in range(int(child.get("GIT_CONFIG_COUNT", "0")))
    }
    assert ("credential.helper", "!stale-helper") not in entries
    assert all(not value.lower().startswith("authorization:") for _, value in entries)


@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        ("true", None, "true"),
        ("false", None, "false"),
        ("true", "false", "false"),
        ("false", "true", "true"),
    ],
)
def test_isolated_child_preserves_parent_command_intent(
    tmp_path: Path,
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    parent: str,
    child: str | None,
    expected: str,
) -> None:
    """An auth-isolated child must not demote the caller's explicit setting."""
    repo = tmp_path / "repo"
    _git(config_env, "init", "--quiet", "--template=", str(repo))
    _git(config_env, "-C", str(repo), "config", "core.symlinks", "false")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.symlinks")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", parent)
    config_env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.symlinks" if child is not None else "credential.helper",
        GIT_CONFIG_VALUE_0=child if child is not None else "",
    )

    result = git_env.git_network_env("https://example.test/org/repo.git", config_env, worktree=repo)

    assert _git(result, "-C", str(repo), "config", "--bool", "core.symlinks") == expected


@pytest.fixture
def symlink_source(tmp_path: Path, config_env: dict[str, str]) -> Path:
    """Commit a real Git symlink without needing OS symlink-create privileges."""
    source = tmp_path / "source"
    _git(config_env, "init", "--quiet", "--template=", str(source))
    (source / "CLAUDE.md").write_text("Package instructions\n", encoding="utf-8")
    _git(config_env, "-C", str(source), "add", "CLAUDE.md")
    oid = _git(config_env, "-C", str(source), "hash-object", "-w", "--stdin", input="CLAUDE.md")
    _git(
        config_env,
        "-C",
        str(source),
        "update-index",
        "--add",
        "--cacheinfo",
        "120000",
        oid,
        "AGENTS.md",
    )
    _git(config_env, "-C", str(source), "commit", "--quiet", "-m", "Symlink fixture")
    _git(config_env, "config", "--global", "core.symlinks", "true")
    return source


def test_clone_respects_unavailable_symlink_capability(
    tmp_path: Path,
    config_env: dict[str, str],
    symlink_source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the non-admin Windows init result, then perform a real checkout."""
    real_init = git_env._git_init_run

    def init_without_symlinks(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        result = real_init(args, **kwargs)
        if "init" in args:
            _git(config_env, "-C", args[-1], "config", "core.symlinks", "false")
        return result

    monkeypatch.setattr(git_env, "_git_init_run", init_without_symlinks)
    target = tmp_path / "clone"

    git_env.clone_git_worktree(str(symlink_source), target, env=config_env)

    assert not (target / "AGENTS.md").is_symlink()
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "CLAUDE.md"
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == "Package instructions\n"
    assert _git(config_env, "config", "--global", "--get", "core.symlinks") == "true"


def test_clone_matches_native_git_symlink_capability(
    tmp_path: Path, config_env: dict[str, str], symlink_source: Path
) -> None:
    """Exercise actual platform detection without mocking Git or OS capability."""
    native = tmp_path / "native"
    target = tmp_path / "apm"
    _git(config_env, "clone", "--quiet", "--template=", str(symlink_source), str(native))

    git_env.clone_git_worktree(str(symlink_source), target, env=config_env)

    assert (target / "AGENTS.md").is_symlink() == (native / "AGENTS.md").is_symlink()
    assert (target / "AGENTS.md").read_bytes() == (native / "AGENTS.md").read_bytes()
    assert _git(config_env, "-C", str(target), "ls-files", "--stage", "AGENTS.md").startswith(
        "120000 "
    )
