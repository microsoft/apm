"""Tests for `apm uninstall` covering the devDependencies.apm section.

Regression trap for #1549: packages installed via `apm install --dev <pkg>`
were stored under `devDependencies.apm` in apm.yml, but `apm uninstall <pkg>`
only scanned `dependencies.apm`. The result was an unconditional
"not found in apm.yml" warning and the dev entry leaking forever.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner


def _write_apm_yml(root: Path, *, deps: list | None = None, dev_deps: list | None = None) -> None:
    """Write an apm.yml with the requested dependency sections."""
    data: dict = {"name": "test-project", "version": "1.0.0", "target": "copilot"}
    if deps is not None:
        data["dependencies"] = {"apm": deps}
    if dev_deps is not None:
        data["devDependencies"] = {"apm": dev_deps}
    (root / "apm.yml").write_text(yaml.dump(data), encoding="utf-8")


def _read_apm_yml(root: Path) -> dict:
    return yaml.safe_load((root / "apm.yml").read_text(encoding="utf-8"))


class TestUninstallDevDependencies:
    """Regression trap for #1549."""

    def test_uninstall_removes_package_from_dev_dependencies(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A package recorded under devDependencies.apm must be removable."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, deps=[], dev_deps=["microsoft/apm-sample-package"])

        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "microsoft/apm-sample-package"])

        assert result.exit_code == 0, result.output
        # "not found in apm.yml" is the bug-mode failure message.
        assert "not found in apm.yml" not in result.output

        data = _read_apm_yml(tmp_path)
        dev_apm = (data.get("devDependencies") or {}).get("apm") or []
        assert "microsoft/apm-sample-package" not in dev_apm, (
            "package should have been removed from devDependencies.apm"
        )

    def test_uninstall_dry_run_finds_dev_dependency(self, tmp_path: Path, monkeypatch) -> None:
        """`--dry-run` must locate dev-only packages too."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, deps=[], dev_deps=["microsoft/apm-sample-package"])

        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "microsoft/apm-sample-package", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "not found in apm.yml" not in result.output
        # apm.yml must be unchanged in dry-run mode.
        data = _read_apm_yml(tmp_path)
        assert data["devDependencies"]["apm"] == ["microsoft/apm-sample-package"]

    def test_uninstall_preserves_unrelated_dev_dependency(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Removing one dev dep must not touch other dev or prod deps."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            deps=["acme/keep-prod"],
            dev_deps=["microsoft/apm-sample-package", "acme/keep-dev"],
        )

        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "microsoft/apm-sample-package"])

        assert result.exit_code == 0, result.output
        data = _read_apm_yml(tmp_path)
        assert data["dependencies"]["apm"] == ["acme/keep-prod"]
        assert data["devDependencies"]["apm"] == ["acme/keep-dev"]

    def test_uninstall_prod_dependency_does_not_synthesize_dev_section(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Removing a prod dep must not add devDependencies to prod-only manifests."""
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, deps=["microsoft/apm-sample-package"])

        from apm_cli.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "microsoft/apm-sample-package"])

        assert result.exit_code == 0, result.output
        data = _read_apm_yml(tmp_path)
        assert "devDependencies" not in data
