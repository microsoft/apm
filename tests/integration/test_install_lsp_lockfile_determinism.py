"""Integration regression for repeated installs with unchanged LSP dependencies."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile


def _write_lsp_manifest(project_root: Path) -> None:
    """Create a project with one root LSP dependency."""
    dep_root = project_root / "packages" / "dep"
    dep_root.mkdir(parents=True)
    (dep_root / "apm.yml").write_text(
        'name: dep\nversion: "1.0.0"\n',
        encoding="utf-8",
    )
    instructions = dep_root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "dep.instructions.md").write_text("# Dependency\n", encoding="utf-8")
    (project_root / "apm.yml").write_text(
        """
name: lsp-lockfile-determinism
version: "1.0.0"
dependencies:
  apm:
    - ./packages/dep
  lsp:
    - name: pyright
      command: pyright-langserver
      extensionToLanguage:
        .py: python
""".lstrip(),
        encoding="utf-8",
    )
    github_dir = project_root / ".github"
    github_dir.mkdir()
    (github_dir / "copilot-instructions.md").write_text("# Test project\n", encoding="utf-8")


def _write_lsp_only_manifest(project_root: Path) -> None:
    """Create a project whose only dependency is an LSP server."""
    (project_root / "apm.yml").write_text(
        """
name: lsp-dry-run
version: "1.0.0"
targets:
  - copilot
dependencies:
  lsp:
    - name: pyright
      command: pyright-langserver
      extensionToLanguage:
        .py: python
""".lstrip(),
        encoding="utf-8",
    )


def _write_unapproved_lsp_manifest(project_root: Path) -> None:
    """Create a project with an unapproved LSP dependency."""
    (project_root / "apm.yml").write_text(
        """
name: lsp-dry-run
version: "1.0.0"
targets:
  - copilot
allowExecutables: {}
dependencies:
  lsp:
    - name: pyright
      command: pyright-langserver
      extensionToLanguage:
        .py: python
""".lstrip(),
        encoding="utf-8",
    )


def _write_apm_only_manifest(project_root: Path) -> None:
    """Create a project whose only dependency is an APM package."""
    dep_root = project_root / "packages" / "dep"
    dep_root.mkdir(parents=True)
    (dep_root / "apm.yml").write_text(
        'name: dep\nversion: "1.0.0"\n',
        encoding="utf-8",
    )
    instructions = dep_root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "dep.instructions.md").write_text("# Dependency\n", encoding="utf-8")
    (project_root / "apm.yml").write_text(
        """
name: apm-dry-run
version: "1.0.0"
dependencies:
  apm:
    - ./packages/dep
""".lstrip(),
        encoding="utf-8",
    )


@patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
def test_lsp_only_dry_run_renders_server_through_install_command(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The command forwards LSP dependencies into its dry-run preview."""
    _write_lsp_only_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "LSP servers to configure (1):" in result.output
    assert "pyright" in result.output
    assert "No dependencies found" not in result.output
    assert "would configure 1 LSP server" in result.output
    assert not (tmp_path / "apm.lock.yaml").exists()


@patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
def test_lsp_only_dry_run_only_apm_reports_selected_empty_plan(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The --only=apm dry run suppresses LSP service configuration."""
    _write_lsp_only_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["install", "--dry-run", "--only=apm"])

    assert result.exit_code == 0, result.output
    assert "LSP servers to configure" not in result.output
    assert "pyright" not in result.output
    assert "No APM dependencies selected by --only=apm" in result.output
    assert not (tmp_path / "apm.lock.yaml").exists()


@patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
def test_apm_only_dry_run_only_mcp_reports_selected_empty_plan(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The --only=mcp dry run suppresses APM package work and cleanup text."""
    _write_apm_only_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["install", "--dry-run", "--only=mcp"])

    assert result.exit_code == 0, result.output
    assert "APM dependencies" not in result.output
    assert "No MCP/LSP dependencies selected by --only=mcp" in result.output
    assert "would clean up stale deployed files" not in result.output
    assert not (tmp_path / "apm.lock.yaml").exists()


@patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
def test_lsp_dry_run_filters_unapproved_server_before_rendering(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Dry-run service counts use the same executable trust filter as install."""
    _write_unapproved_lsp_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Filtered 1 LSP server(s)" in result.output
    assert "LSP servers to configure" not in result.output
    assert "No dependencies found" in result.output
    assert "would make no changes" in result.output
    assert not (tmp_path / "apm.lock.yaml").exists()


@patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
def test_repeated_install_with_unchanged_lsp_keeps_lockfile_bytes(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A second real CLI install must leave the LSP lockfile byte-identical."""
    _write_lsp_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    first_result = runner.invoke(cli, ["install", "--target", "copilot"])
    assert first_result.exit_code == 0, first_result.output

    lock_path = tmp_path / "apm.lock.yaml"
    first_bytes = lock_path.read_bytes()
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_lock.generated_at is None
    assert first_lock.lsp_servers == ["pyright"]

    second_result = runner.invoke(cli, ["install", "--target", "copilot"])
    assert second_result.exit_code == 0, second_result.output

    second_lock = LockFile.read(lock_path)
    assert second_lock is not None
    assert second_lock.generated_at == first_lock.generated_at
    assert second_lock.lsp_servers == first_lock.lsp_servers
    assert second_lock.lsp_configs == first_lock.lsp_configs
    assert lock_path.read_bytes() == first_bytes
