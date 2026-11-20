"""Real Git checks for the pinned ``core.symlinks=false`` dependency contract (#3457)."""

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils import git_env
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.file_ops import robust_copytree

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


_QUERY = ("config", "--bool", "--get", "core.symlinks")


def _posix_package_hash(tmp_path: Path) -> str:
    """Hash the dereferenced tree that a POSIX install records for the fixture."""
    expected = tmp_path / "posix-expected"
    expected.mkdir()
    (expected / "CLAUDE.md").write_bytes(b"Package instructions\n")
    (expected / "AGENTS.md").write_bytes(b"Package instructions\n")
    return compute_package_hash(expected)


@pytest.mark.parametrize("inherited_scope", ["system", "global"])
@pytest.mark.parametrize(
    ("inherited", "local_scope", "local", "command_source", "command"),
    [
        ("true", "local", "false", None, None),
        ("false", "local", "true", None, None),
        ("true", "worktree", "false", None, None),
        ("true", "local", "false", "indexed", "false"),
        ("true", "local", "false", "parameters", "false"),
        ("false", "worktree", "true", "indexed", "false"),
        ("true", "local", None, None, None),
        ("false", "local", None, None, None),
    ],
)
def test_network_env_pins_link_text(
    tmp_path: Path,
    config_env: dict[str, str],
    inherited_scope: str,
    inherited: str,
    local_scope: str,
    local: str | None,
    command_source: str | None,
    command: str | None,
) -> None:
    """No inherited, capability or caller value can make Git create OS symlinks."""
    config = config_env[f"GIT_CONFIG_{inherited_scope.upper()}"]
    _git(config_env, "config", "--file", config, "core.symlinks", inherited)
    _git(config_env, "config", "--file", config, "core.compression", "3")
    original_config = Path(config).read_bytes()
    repo = tmp_path / "repo"
    _git(config_env, "init", "--quiet", "--template=", str(repo))
    # Normalize the native init outcome so every case runs on every OS.
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

    child = git_env.git_network_env("https://example.test/org/repo.git", config_env, worktree=repo)

    assert _git(child, "-C", str(repo), *_QUERY) == "false"
    assert _git(child, "-C", str(repo), "config", "--get", "core.compression") == "3"
    assert child["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child["GIT_CONFIG_SYSTEM"] == os.devnull
    assert Path(config).read_bytes() == original_config


def test_network_env_symlink_pin_preserves_auth_isolation(
    tmp_path: Path,
    config_env: dict[str, str],
) -> None:
    """The pin must not resurrect scrubbed auth state from interleaved entries."""
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

    assert _git(child, "-C", str(repo), *_QUERY) == "false"
    assert "GIT_HTTP_EXTRAHEADER" not in child
    entries = [
        (
            child.get(f"GIT_CONFIG_KEY_{index}", ""),
            child.get(f"GIT_CONFIG_VALUE_{index}", ""),
        )
        for index in range(int(child.get("GIT_CONFIG_COUNT", "0")))
    ]
    assert ("credential.helper", "!stale-helper") not in entries
    assert all(not value.lower().startswith("authorization:") for _, value in entries)
    assert [entry for entry in entries if entry[0] == "core.symlinks"] == [
        ("core.symlinks", "false")
    ]


@pytest.mark.parametrize(("parent", "child"), [("true", None), ("true", "true")])
def test_isolated_child_pins_over_parent_command_intent(
    tmp_path: Path,
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    parent: str,
    child: str | None,
) -> None:
    """Merged parent and child snapshots still resolve to the pinned value."""
    repo = tmp_path / "repo"
    _git(config_env, "init", "--quiet", "--template=", str(repo))
    _git(config_env, "-C", str(repo), "config", "core.symlinks", "true")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.symlinks")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", parent)
    config_env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.symlinks" if child is not None else "credential.helper",
        GIT_CONFIG_VALUE_0=child if child is not None else "",
    )

    result = git_env.git_network_env("https://example.test/org/repo.git", config_env, worktree=repo)

    assert _git(result, "-C", str(repo), *_QUERY) == "false"


@pytest.fixture
def symlink_source(tmp_path: Path, config_env: dict[str, str]) -> Path:
    """Commit a real Git symlink without needing OS symlink-create privileges."""
    source = tmp_path / "source"
    _git(config_env, "init", "--quiet", "--template=", str(source))
    (source / "CLAUDE.md").write_bytes(b"Package instructions\n")
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


def test_clone_writes_link_text_even_when_host_config_asks_for_symlinks(
    tmp_path: Path, config_env: dict[str, str], symlink_source: Path
) -> None:
    """The Git layer never creates an OS link, whatever the host config says."""
    target = tmp_path / "clone"

    git_env.clone_git_worktree(str(symlink_source), target, env=config_env)

    assert not (target / "AGENTS.md").is_symlink()
    assert (target / "AGENTS.md").read_bytes() == b"CLAUDE.md"
    assert _git(config_env, "config", "--global", "--get", "core.symlinks") == "true"


def test_clone_then_materialize_records_the_posix_content_hash(
    tmp_path: Path, config_env: dict[str, str], symlink_source: Path
) -> None:
    """Link text plus Git-object materialization hashes exactly as POSIX does."""
    from apm_cli.utils.git_sparse import materialize_tracked_symlinks

    clone = tmp_path / "clone"
    installed = tmp_path / "apm_modules" / "pkg"

    git_env.clone_git_worktree(str(symlink_source), clone, env=config_env)
    materialize_tracked_symlinks("git", clone, None, env=config_env)
    robust_copytree(clone, installed, ignore=lambda _root, names: {".git"} & set(names))

    assert (installed / "AGENTS.md").read_bytes() == b"Package instructions\n"
    assert compute_package_hash(installed) == _posix_package_hash(tmp_path)
