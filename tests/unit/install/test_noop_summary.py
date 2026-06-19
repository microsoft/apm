"""Regression coverage for no-op install summary rendering."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
    clear_apm_yml_cache,
)
from apm_cli.models.dependency.reference import DependencyReference

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


class _StableDownloader:
    """Downloader stub whose second install must be a cache/no-op path."""

    def __init__(self) -> None:
        self.calls = 0

    def download_package(
        self,
        repo_ref: object,
        target_path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> PackageInfo:
        self.calls += 1
        dep_ref = (
            repo_ref
            if isinstance(repo_ref, DependencyReference)
            else DependencyReference.parse(str(repo_ref))
        )
        target_path = Path(target_path)
        target_path.mkdir(parents=True, exist_ok=True)
        (target_path / "apm.yml").write_text(
            yaml.safe_dump({"name": "fixture-pkg", "version": "1.0.0"}),
            encoding="utf-8",
        )
        instructions_dir = target_path / ".apm" / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)
        (instructions_dir / "fixture.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Fixture\n",
            encoding="utf-8",
        )
        package = APMPackage.from_apm_yml(target_path / "apm.yml")
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            resolved_reference=ResolvedReference(
                original_ref="v1.0.0",
                ref_type=GitReferenceType.TAG,
                resolved_commit="abc123",
                ref_name="v1.0.0",
            ),
        )


@pytest.fixture(autouse=True)
def _clear_package_cache() -> None:
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text("# Project\n", encoding="utf-8")
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "noop-summary",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": ["acme/fixture-pkg#v1.0.0"], "mcp": []},
            }
        ),
        encoding="utf-8",
    )


def _run_install(runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, ["install"], catch_exceptions=False)


def test_second_noop_install_reports_no_changes_in_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache/no-op reinstall must not claim a dependency was installed."""
    project = tmp_path / "project"
    _write_project(project)
    downloader = _StableDownloader()

    from apm_cli.deps import github_downloader as _ghd

    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader,
        "download_package",
        downloader.download_package,
    )
    runner = CliRunner()

    first = _run_install(runner, project, monkeypatch)
    assert first.exit_code == 0, first.output
    assert downloader.calls == 1

    second = _run_install(runner, project, monkeypatch)
    assert second.exit_code == 0, second.output
    assert downloader.calls == 1
    assert "(files unchanged)" in second.output
    assert "Installed 1 APM dependency" not in second.output
    assert "No changes" in second.output
