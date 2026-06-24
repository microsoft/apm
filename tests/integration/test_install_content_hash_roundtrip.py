"""Integration coverage for content-hash-only lockfile replay.

The no-``resolved_commit`` case happens for unpinned git dependencies whose
clone path cannot provide a stable commit anchor. The lockfile still records a
package ``content_hash``; a second install must trust only that hash and must not
re-download unchanged on-disk bytes.
"""

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
)
from apm_cli.models.dependency.reference import DependencyReference

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


class _ContentHashOnlyDownloader:
    """Downloader stub that changes bytes if a second download occurs."""

    def __init__(self) -> None:
        self.calls = 0

    def download_package(
        self, repo_ref: object, target_path: Path, *args: Any, **kwargs: Any
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
            yaml.safe_dump(
                {
                    "name": "fixture-pkg",
                    "version": "1.0.0",
                    "description": "content hash replay fixture",
                }
            ),
            encoding="utf-8",
        )
        (target_path / ".apm" / "instructions").mkdir(parents=True, exist_ok=True)
        (target_path / ".apm" / "instructions" / "fixture.instructions.md").write_text(
            f"---\napplyTo: '**'\n---\n# Fixture\ndownload-call: {self.calls}\n",
            encoding="utf-8",
        )
        package = APMPackage.from_apm_yml(target_path / "apm.yml")
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            resolved_reference=ResolvedReference(
                original_ref="default",
                ref_type=GitReferenceType.BRANCH,
                resolved_commit=None,
                ref_name="default",
            ),
        )


def _write_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text("# Project\n", encoding="utf-8")
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "content-hash-roundtrip",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": ["acme/fixture-pkg"], "mcp": []},
            }
        ),
        encoding="utf-8",
    )


def _run_install(runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, ["install"], catch_exceptions=False)


def _locked_dep(project: Path) -> dict:
    lockfile = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    deps = lockfile.get("dependencies") or []
    return next(dep for dep in deps if dep.get("repo_url") == "acme/fixture-pkg")


def test_no_resolved_commit_content_hash_reuses_on_disk_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second install reuses content-hash-verified bytes without re-downloading.

    The downloader deliberately mutates fixture content on every call. If the
    second install re-downloads instead of reusing the content-hash-verified
    install path, the fresh-download supply-chain check sees a different hash
    from the lockfile and the install fails.
    """
    project = tmp_path / "project"
    _write_project(project)
    downloader = _ContentHashOnlyDownloader()

    from apm_cli.deps import github_downloader as _ghd

    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader, "download_package", downloader.download_package
    )
    runner = CliRunner()

    first = _run_install(runner, project, monkeypatch)
    assert first.exit_code == 0, first.output

    locked = _locked_dep(project)
    assert locked.get("content_hash"), locked
    assert not locked.get("resolved_commit"), locked
    assert downloader.calls == 1

    second = _run_install(runner, project, monkeypatch)
    assert second.exit_code == 0, second.output
    assert downloader.calls == 1
