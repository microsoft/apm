"""Integration regression for repeated installs with unchanged LSP dependencies."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.lsp_integrator import LSPIntegrator


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


def _write_project_lsp_with_executable_gate(project_root: Path) -> None:
    """Create a project-owned LSP alongside an enabled package trust gate."""
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


def _write_untrusted_package_lsp_fixture(project_root: Path) -> None:
    """Seed a local package and its installed copy without trusted executables."""
    package_manifest = """
name: untrusted-lsp-package
version: "1.0.0"
dependencies:
  lsp:
    - name: package-pyright
      command: pyright-langserver
      extensionToLanguage:
        .py: python
""".lstrip()
    source_package = project_root / "packages" / "untrusted-lsp-package"
    source_package.mkdir(parents=True)
    (source_package / "apm.yml").write_text(package_manifest, encoding="utf-8")

    installed_package = project_root / "apm_modules" / "_local" / "untrusted-lsp-package"
    installed_package.mkdir(parents=True)
    (installed_package / "apm.yml").write_text(package_manifest, encoding="utf-8")

    (project_root / "apm.yml").write_text(
        """
name: lsp-package-gate
version: "1.0.0"
targets:
  - copilot
allowExecutables: {}
dependencies:
  apm:
    - ./packages/untrusted-lsp-package
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
def test_lsp_dry_run_keeps_project_owned_server_when_package_gate_is_enabled(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The package trust gate does not hide a project-authored LSP server."""
    _write_project_lsp_with_executable_gate(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Filtered" not in result.output
    assert "LSP servers to configure (1):" in result.output
    assert "pyright" in result.output
    assert "would configure 1 LSP server" in result.output
    assert not (tmp_path / "apm.lock.yaml").exists()
    assert not (tmp_path / ".github" / "lsp.json").exists()


@patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
def test_lsp_dry_run_filters_unapproved_dependency_server(
    _mock_updates,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The CLI preview filters package-owned data at its model-loading boundary."""
    _write_untrusted_package_lsp_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    dependencies = LSPIntegrator.collect_transitive(tmp_path / "apm_modules")
    assert len(dependencies) == 1
    assert dependencies[0].resolved_by == "_local/untrusted-lsp-package"
    # Dry-run does not discover transitive services. Supply the real collector's
    # ownership-bearing result at the existing package-model boundary instead.
    with patch(
        "apm_cli.models.apm_package.APMPackage.get_lsp_dependencies",
        return_value=dependencies,
    ):
        result = CliRunner().invoke(cli, ["install", "--dry-run", "--no-policy"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "Filtered 1 LSP server from '_local/untrusted-lsp-package'" in output
    assert "declaring package is not trusted yet" in output
    assert "LSP servers to configure" not in output
    assert "package-pyright" not in output
    assert not (tmp_path / "apm.lock.yaml").exists()
    assert not (tmp_path / ".github" / "lsp.json").exists()


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
