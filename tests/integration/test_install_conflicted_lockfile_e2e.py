"""End-to-end diagnostics for git merge conflict markers in ``apm.lock.yaml`` (#2979).

Every command that reads the lockfile reports the conflict by name and leaves
the file byte-for-byte intact. Automatic recovery is deliberately not part of
this contract: no command discards, rewrites, or re-resolves the conflicted
file.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockFile, LockfileConflictError
from apm_cli.models.apm_package import clear_apm_yml_cache
from tests.utils.diagnostic_recipe import run_recipe, shell_commands_in

pytestmark = pytest.mark.component

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"

_CONFLICTED_LOCKFILE = textwrap.dedent("""\
    lockfile_version: '1'
    <<<<<<< HEAD
    dependencies: []
    =======
    dependencies:
    - repo_url: example/x
    >>>>>>> feature
""")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


@pytest.fixture
def conflicted_project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        textwrap.dedent("""\
            name: test-project
            version: '1.0.0'
            targets:
              - claude
        """),
        encoding="utf-8",
    )
    instructions = tmp_path / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "hi.instructions.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / "apm.lock.yaml").write_text(_CONFLICTED_LOCKFILE, encoding="utf-8")
    return tmp_path


def _invoke(runner: CliRunner, args: list[str], *, catch_exceptions: bool = False):
    from apm_cli.cli import cli

    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, args, catch_exceptions=catch_exceptions)


def _combined_output(result) -> str:
    """Whitespace-collapsed output, for substring assertions across wrapped lines."""
    return " ".join(_raw_output(result).split())


def _raw_output(result) -> str:
    """Output with layout intact, for anything that reads the printed recipe."""
    return (result.output or "") + (result.stderr or "")


_MUTATING_COMMANDS = [
    ["install"],
    ["install", "--frozen"],
    ["install", "--only", "apm"],
    ["install", "--only", "mcp"],
    ["install", "--mcp", "foo", "--url", "http://127.0.0.1:1/mcp"],
    ["lock"],
]


@pytest.mark.parametrize("args", _MUTATING_COMMANDS)
def test_commands_fail_closed_and_preserve_the_lockfile(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    result = _invoke(runner, args)

    assert result.exit_code == 1, result.output
    output = _combined_output(result)
    assert "conflict markers" in output
    assert shell_commands_in(_raw_output(result)), "the diagnostic MUST offer a runnable recovery"
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE, "the conflicted bytes MUST be preserved"


@pytest.mark.parametrize("args", _MUTATING_COMMANDS)
def test_commands_never_recommend_a_command_against_the_same_file(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    """The old failure mode: advice the user cannot act on while the file is unreadable."""
    result = _invoke(runner, args)

    output = _combined_output(result)
    assert "apm outdated" not in output
    assert "apm update" not in output


@pytest.mark.parametrize("args", [["update"], ["outdated"], ["lock", "export"]])
def test_read_only_commands_name_the_conflict(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    """These let the error reach ``main()``, which prints ``Error: {exc}``."""
    result = _invoke(runner, args, catch_exceptions=True)

    assert result.exit_code == 1
    assert isinstance(result.exception, LockfileConflictError)
    message = str(result.exception)
    assert "contains unresolved git merge conflict markers" in message
    assert "could not find expected" not in message, "no raw parser content"
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE


def test_dry_run_names_the_conflict_and_preserves_the_file(
    runner: CliRunner, conflicted_project: Path
) -> None:
    """A preview performs no durable write, so it warns and keeps its exit code."""
    result = _invoke(runner, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "conflict markers" in _combined_output(result)
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE


def test_corrupt_lockfile_without_markers_is_unchanged(
    runner: CliRunner, conflicted_project: Path
) -> None:
    lockfile_path = conflicted_project / "apm.lock.yaml"
    corrupt = "lockfile_version: '1'\ndependencies: [\n"
    lockfile_path.write_text(corrupt, encoding="utf-8")

    result = _invoke(runner, ["install"])

    assert result.exit_code == 1, result.output
    assert "conflict markers" not in _combined_output(result)
    assert lockfile_path.read_text(encoding="utf-8") == corrupt


def test_valid_lockfile_still_installs(runner: CliRunner, conflicted_project: Path) -> None:
    """The detector must not fire on an ordinary lockfile."""
    (conflicted_project / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\ndependencies: []\n", encoding="utf-8"
    )

    result = _invoke(runner, ["install"])

    assert result.exit_code == 0, result.output
    assert "conflict markers" not in _combined_output(result)


def _git(args: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", **_git_env()},
    )


def _git_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update(
        {
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return env


def test_the_printed_recovery_commands_actually_resolve_the_lockfile(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    """Run what the diagnostic prints; the lockfile must become readable.

    This asserts the advice works rather than how it is worded, so the message
    can be rephrased freely and this test still fails if it stops being correct.
    """
    project = tmp_path / "repo"
    (project / ".apm" / "instructions").mkdir(parents=True)
    (project / "apm.yml").write_text(
        "name: recipe\nversion: '1.0.0'\ntargets:\n  - claude\n", encoding="utf-8"
    )
    (project / ".apm" / "instructions" / "base.instructions.md").write_text("base\n")
    monkeypatch.chdir(project)

    _git(["init", "-q", "."], project)
    _invoke(runner, ["install"])
    _git(["add", "-A"], project)
    _git(["commit", "-qm", "base"], project)

    _git(["checkout", "-qb", "side-a"], project)
    (project / ".apm" / "instructions" / "a.instructions.md").write_text("a\n")
    clear_apm_yml_cache()
    _invoke(runner, ["install"])
    _git(["add", "-A"], project)
    _git(["commit", "-qm", "a"], project)

    _git(["checkout", "-q", "-"], project)
    _git(["checkout", "-qb", "side-b"], project)
    (project / ".apm" / "instructions" / "b.instructions.md").write_text("b\n")
    clear_apm_yml_cache()
    _invoke(runner, ["install"])
    _git(["add", "-A"], project)
    _git(["commit", "-qm", "b"], project)

    import subprocess

    subprocess.run(["git", "merge", "side-a"], cwd=str(project), capture_output=True)
    lockfile_path = project / "apm.lock.yaml"
    conflicted = lockfile_path.read_text(encoding="utf-8")
    assert "<<<<<<<" in conflicted, "the merge must leave real conflict markers"

    clear_apm_yml_cache()
    result = _invoke(runner, ["install"], catch_exceptions=True)
    commands = shell_commands_in(_raw_output(result))
    assert commands, f"the diagnostic MUST print a recovery recipe; got:\n{result.output}"

    run_recipe(commands, project, only="git")

    assert LockFile.read(lockfile_path) is not None, (
        "after running the printed recovery, the lockfile MUST be readable"
    )
