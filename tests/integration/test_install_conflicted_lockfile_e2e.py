"""Command-boundary diagnostics for lockfile conflict markers (#2979).

Every command that reads the lockfile reports the conflict by name and leaves
the file byte-for-byte intact. Automatic recovery is deliberately not part of
this contract: no command discards, rewrites, or re-resolves the conflicted
file.
"""

from __future__ import annotations

import json
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


@pytest.fixture(params=["\n", "\r\n"], ids=["lf", "crlf"])
def conflicted_project(tmp_path: Path, monkeypatch, request) -> Path:
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
    (tmp_path / "apm.lock.yaml").write_bytes(
        _CONFLICTED_LOCKFILE.replace("\n", request.param).encode("utf-8")
    )
    return tmp_path


def _invoke(runner: CliRunner, args: list[str], *, catch_exceptions: bool = False):
    from apm_cli.cli import cli

    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, args, catch_exceptions=catch_exceptions)


def _combined_output(result) -> str:
    """Whitespace-collapsed output, for substring assertions across wrapped lines."""
    return " ".join(_raw_output(result).split())


def _raw_output(result) -> str:
    """Combine stdout and stderr without discarding diagnostic text."""
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
    lockfile_path = conflicted_project / "apm.lock.yaml"
    original = lockfile_path.read_bytes()
    result = _invoke(runner, args)

    assert result.exit_code == 1, result.output
    output = _combined_output(result)
    assert "conflict markers" in output
    assert "apm.lock.yaml" in output
    assert "restore a known-good lockfile" in output
    assert "retry your original command" in output
    assert "git checkout" not in output
    # The original bug: advice the reader cannot act on while the file is unreadable.
    assert "apm outdated" not in output
    assert "apm update" not in output
    assert lockfile_path.read_bytes() == original, "the conflicted bytes MUST be preserved"


@pytest.mark.parametrize("args", [["update"], ["outdated"], ["lock", "export"]])
def test_read_only_commands_name_the_conflict(
    runner: CliRunner, conflicted_project: Path, args: list[str]
) -> None:
    """These let the error reach ``main()``, which prints ``Error: {exc}``."""
    lockfile_path = conflicted_project / "apm.lock.yaml"
    original = lockfile_path.read_bytes()
    result = _invoke(runner, args, catch_exceptions=True)

    assert result.exit_code == 1
    assert isinstance(result.exception, LockfileConflictError)
    message = str(result.exception)
    assert "contains unresolved git merge conflict markers" in message
    assert "could not find expected" not in message, "no raw parser content"
    assert str(lockfile_path) in message
    assert "restore a known-good lockfile" in message
    assert lockfile_path.read_bytes() == original


def test_dry_run_names_the_conflict_and_preserves_the_file(
    runner: CliRunner, conflicted_project: Path
) -> None:
    """A preview performs no durable write, so it warns and keeps its exit code."""
    lockfile_path = conflicted_project / "apm.lock.yaml"
    original = lockfile_path.read_bytes()
    result = _invoke(runner, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "conflict markers" in _combined_output(result)
    assert "retry your original command" in _combined_output(result)
    assert lockfile_path.read_bytes() == original


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


@pytest.mark.parametrize("lockfile_name", ["apm.lock.yaml", "apm.lock"])
def test_export_from_ancestor_preserves_the_conflicted_lockfile(
    runner: CliRunner, conflicted_project: Path, monkeypatch, lockfile_name: str
) -> None:
    """Read-only export uses the canonical loader without migrating legacy input."""
    modern = conflicted_project / "apm.lock.yaml"
    path = conflicted_project / lockfile_name
    if path != modern:
        modern.rename(path)
    original = path.read_bytes()
    nested = conflicted_project / "space $and #hash"
    nested.mkdir()
    monkeypatch.chdir(nested)

    result = _invoke(runner, ["lock", "export"], catch_exceptions=True)

    assert result.exit_code == 1
    assert isinstance(result.exception, LockfileConflictError)
    assert result.exception.path == path
    assert "retry your original command" in str(result.exception)
    assert path.read_bytes() == original
    assert not (nested / "apm.lock.yaml").exists()
    if path != modern:
        assert not modern.exists()


@pytest.mark.parametrize(
    "args",
    [["install", "--global"], ["install", "--global", "--frozen"], ["lock", "export", "--global"]],
)
def test_global_commands_diagnose_only_the_user_lockfile(
    runner: CliRunner, conflicted_project: Path, monkeypatch, args: list[str]
) -> None:
    """Global errors identify and preserve user state without repairing project state."""
    home = conflicted_project / "home $with #spaces"
    user_root = home / ".apm"
    user_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (user_root / "apm.yml").write_bytes((conflicted_project / "apm.yml").read_bytes())
    project_lock = conflicted_project / "apm.lock.yaml"
    user_lock = user_root / "apm.lock.yaml"
    original = project_lock.read_bytes()
    user_lock.write_bytes(original)
    project_lock.write_bytes(b"lockfile_version: '1'\ndependencies: []\n")
    project_before = project_lock.read_bytes()

    result = _invoke(runner, args, catch_exceptions=True)

    assert result.exit_code == 1, result.output
    output = _combined_output(result)
    if isinstance(result.exception, LockfileConflictError):
        assert result.exception.path == user_lock
        output += str(result.exception)
    assert "".join(str(user_lock).split()) in "".join(output.split())
    assert "conflict markers" in output
    assert "restore a known-good lockfile" in output
    assert "retry your original command" in output
    assert user_lock.read_bytes() == original
    assert project_lock.read_bytes() == project_before


def test_manual_restore_allows_retry_without_a_git_merge(
    runner: CliRunner, conflicted_project: Path
) -> None:
    """Restoring known-good bytes makes the original read-only command work."""
    result = _invoke(runner, ["lock", "export"], catch_exceptions=True)
    assert result.exit_code == 1
    assert isinstance(result.exception, LockfileConflictError)
    assert "restore a known-good lockfile" in str(result.exception)
    assert not (conflicted_project / ".git").exists()

    known_good = b"lockfile_version: '1'\ndependencies: []\n"
    path = conflicted_project / "apm.lock.yaml"
    path.write_bytes(known_good)

    result = _invoke(runner, ["lock", "export"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["bomFormat"] == "CycloneDX"
    assert path.read_bytes() == known_good


def test_export_prefers_current_filename_without_migrating_legacy(
    runner: CliRunner, conflicted_project: Path
) -> None:
    """An unused legacy conflict must not override a valid current lockfile."""
    legacy = conflicted_project / "apm.lock"
    modern = conflicted_project / "apm.lock.yaml"
    original = modern.read_bytes()
    legacy.write_bytes(original)
    current = b"lockfile_version: '1'\ndependencies: []\n"
    modern.write_bytes(current)

    result = _invoke(runner, ["lock", "export"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["bomFormat"] == "CycloneDX"
    assert legacy.read_bytes() == original
    assert modern.read_bytes() == current


def test_best_effort_installed_paths_preserves_conflicted_bytes(conflicted_project: Path) -> None:
    """Inventory discovery retains its existing empty-result corruption policy."""
    path = conflicted_project / "apm.lock.yaml"
    original = path.read_bytes()

    assert LockFile.installed_paths_for_project(conflicted_project) == []
    assert path.read_bytes() == original
