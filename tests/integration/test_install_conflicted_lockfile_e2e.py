"""End-to-end recovery from git merge conflict markers in ``apm.lock.yaml`` (#2979).

A full ``apm install`` and ``apm lock`` discard the lockfile and resolve from
``apm.yml`` with a warning. ``--frozen``, partial installs, and read-only
commands fail closed with an error that names the file and a working next
action.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockFile, LockfileConflictError
from apm_cli.models.apm_package import clear_apm_yml_cache

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
    return " ".join(((result.output or "") + (result.stderr or "")).split())


@pytest.mark.parametrize("args", [["install"], ["lock"]])
def test_regenerates_lockfile_with_warning(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    result = _invoke(runner, args)

    assert result.exit_code == 0, result.output
    assert "conflict markers" in _combined_output(result)
    assert "resolving from apm.yml" in _combined_output(result)
    lockfile_path = conflicted_project / "apm.lock.yaml"
    assert "<<<<<<<" not in lockfile_path.read_text(encoding="utf-8")
    lock = LockFile.read(lockfile_path)
    assert lock is not None
    assert lock.get_package_dependencies() == []


def test_install_records_local_content_after_regeneration(
    runner: CliRunner, conflicted_project: Path
) -> None:
    result = _invoke(runner, ["install"])

    assert result.exit_code == 0, result.output
    lock = LockFile.read(conflicted_project / "apm.lock.yaml")
    assert lock is not None
    assert lock.local_deployed_files == [".claude/rules/hi.md"]


def test_frozen_install_fails_closed_and_leaves_file_untouched(
    runner: CliRunner, conflicted_project: Path
) -> None:
    result = _invoke(runner, ["install", "--frozen"])

    assert result.exit_code == 1
    output = _combined_output(result)
    assert "conflict markers" in output
    assert "without --frozen" in output
    assert "apm outdated" not in output
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE


@pytest.mark.parametrize("args", [["update"], ["outdated"], ["lock", "export"]])
def test_read_only_commands_name_the_conflict(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    """These commands let the error reach ``main()``, which prints ``Error: {exc}``."""
    result = _invoke(runner, args, catch_exceptions=True)

    assert result.exit_code == 1
    assert isinstance(result.exception, LockfileConflictError)
    message = str(result.exception)
    assert "apm.lock.yaml contains git merge conflict markers" in message
    assert "run 'apm install'" in message
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE


@pytest.mark.parametrize(
    "args",
    [
        ["install", "./pkg"],
        ["install", "--only", "apm"],
        ["install", "--only", "mcp"],
        ["install", "--mcp", "foo", "--url", "http://127.0.0.1:1/mcp"],
    ],
)
def test_partial_installs_fail_closed_and_name_the_conflict(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    """A partial install cannot re-resolve every apm.yml entry, so it never discards."""
    pkg = conflicted_project / "pkg" / ".apm" / "instructions"
    pkg.mkdir(parents=True)
    (conflicted_project / "pkg" / "apm.yml").write_text("name: pkg\nversion: '1.0.0'\n")
    (pkg / "p.instructions.md").write_text("pkg\n", encoding="utf-8")

    result = _invoke(runner, args)

    assert result.exit_code == 1, result.output
    output = _combined_output(result)
    assert "apm.lock.yaml contains git merge conflict markers" in output
    assert "run 'apm install'" in output
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE


def test_dry_run_names_the_conflict_without_touching_the_file(
    runner: CliRunner, conflicted_project: Path
) -> None:
    result = _invoke(runner, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "apm.lock.yaml contains git merge conflict markers" in _combined_output(result)
    lockfile_text = (conflicted_project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert lockfile_text == _CONFLICTED_LOCKFILE


def test_corrupt_lockfile_without_markers_still_fails_closed(
    runner: CliRunner, conflicted_project: Path
) -> None:
    lockfile_path = conflicted_project / "apm.lock.yaml"
    corrupt = "lockfile_version: '1'\ndependencies: [\n"
    lockfile_path.write_text(corrupt, encoding="utf-8")

    result = _invoke(runner, ["install"])

    assert result.exit_code == 1, result.output
    assert "conflict markers" not in _combined_output(result)
    assert lockfile_path.read_text(encoding="utf-8") == corrupt
