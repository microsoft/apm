"""``apm manifest migrate`` command contract (#3762)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.manifest import manifest

_LEGACY = "name: demo\nversion: 1.0.0\n# deploy\ntarget: claude,agents  # two\n"
_MIGRATED = "name: demo\nversion: 1.0.0\n# deploy\ntargets: [claude, copilot] # two\n"


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_HOME", str(tmp_path / "user-home"))
    monkeypatch.delenv("CI", raising=False)


def _invoke(*args: str):
    return CliRunner().invoke(manifest, ["migrate", *args])


def _write(path: Path, text: str = _LEGACY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_dry_run_prints_diff_and_does_not_write(tmp_path):
    manifest_path = _write(tmp_path / "apm.yml")

    result = _invoke("--dry-run")

    assert result.exit_code == 0, result.output
    assert "-target: claude,agents  # two" in result.output
    assert "+targets: [claude, copilot] # two" in result.output
    assert manifest_path.read_text(encoding="utf-8") == _LEGACY


def test_yes_writes_migrated_manifest_then_is_idempotent(tmp_path):
    manifest_path = _write(tmp_path / "apm.yml")

    first = _invoke("--yes")
    second = _invoke("--yes")

    assert first.exit_code == 0, first.output
    assert "[+] Migrated apm.yml: 'target:' is now 'targets:'" in first.output
    assert manifest_path.read_text(encoding="utf-8") == _MIGRATED
    assert second.exit_code == 0, second.output
    assert "[+] apm.yml is up to date -- nothing to migrate" in second.output
    assert manifest_path.read_text(encoding="utf-8") == _MIGRATED


@pytest.mark.parametrize("args", [(), ("--yes",), ("--dry-run",)])
def test_ci_prints_diff_and_exits_1_without_writing(tmp_path, monkeypatch, args):
    manifest_path = _write(tmp_path / "apm.yml")
    monkeypatch.setenv("CI", "true")

    result = _invoke(*args)

    assert result.exit_code == 1
    assert "+targets: [claude, copilot] # two" in result.output
    assert "CI never writes the manifest." in " ".join(result.output.split())
    assert manifest_path.read_text(encoding="utf-8") == _LEGACY


def test_ci_on_migrated_manifest_exits_0(tmp_path, monkeypatch):
    _write(tmp_path / "apm.yml", _MIGRATED)
    monkeypatch.setenv("CI", "true")

    assert _invoke().exit_code == 0


def test_non_interactive_without_yes_refuses_to_write(tmp_path):
    manifest_path = _write(tmp_path / "apm.yml")

    result = _invoke()

    assert result.exit_code == 1
    assert "Re-run with --yes to apply." in " ".join(result.output.split())
    assert manifest_path.read_text(encoding="utf-8") == _LEGACY


def test_unmappable_target_is_refused(tmp_path):
    original = "name: demo\nversion: 1.0.0\ntarget: intellij\n"
    manifest_path = _write(tmp_path / "apm.yml", original)

    result = _invoke("--yes")

    assert result.exit_code == 1
    assert (
        "[x] 'target: intellij' has no 'targets:' equivalent; remove it, or pass "
        "'--target intellij' on the command line" in " ".join(result.output.split())
    )
    assert manifest_path.read_text(encoding="utf-8") == original


def test_target_all_prints_pruning_hint(tmp_path):
    _write(tmp_path / "apm.yml", "name: demo\nversion: 1.0.0\ntarget: all\n")

    result = _invoke("--dry-run")

    assert result.exit_code == 0, result.output
    assert "'target: all' becomes the 9 targets apm install deploys today." in " ".join(
        result.output.split()
    )


def test_global_migrates_the_user_manifest_only(tmp_path):
    project = _write(tmp_path / "apm.yml")
    user = _write(tmp_path / "user-home" / "apm.yml")

    result = _invoke("--global", "--yes")

    assert result.exit_code == 0, result.output
    assert user.read_text(encoding="utf-8") == _MIGRATED
    assert project.read_text(encoding="utf-8") == _LEGACY


def test_missing_manifest_exits_1(tmp_path):
    result = _invoke("--dry-run")

    assert result.exit_code == 1
    assert "No manifest found at" in result.output
