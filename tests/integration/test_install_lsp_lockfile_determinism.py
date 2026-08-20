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
    assert first_lock.lsp_servers == ["pyright"]

    second_result = runner.invoke(cli, ["install", "--target", "copilot"])
    assert second_result.exit_code == 0, second_result.output

    second_lock = LockFile.read(lock_path)
    assert second_lock is not None
    assert second_lock.generated_at == first_lock.generated_at
    assert second_lock.lsp_servers == first_lock.lsp_servers
    assert second_lock.lsp_configs == first_lock.lsp_configs
    assert lock_path.read_bytes() == first_bytes
