"""Integration coverage for content-hash-only lockfile replay.

The no-``resolved_commit`` case happens for unpinned git dependencies whose
clone path cannot provide a stable commit anchor. The lockfile still records a
package ``content_hash``; a second install must trust only that hash and must not
re-download unchanged on-disk bytes.
"""

from __future__ import annotations

import json
import shutil
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
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_VIRTUAL_COMMIT = "a" * 40
_VIRTUAL_DEPENDENCY = "acme/fixture/instructions/fixture.instructions.md#" + _VIRTUAL_COMMIT
_VIRTUAL_SOURCE = b"# Fixture\nsame payload\n"


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


def _run_command(
    runner: CliRunner,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> object:
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        return runner.invoke(cli, args, catch_exceptions=False)


def _locked_dep(project: Path) -> dict:
    lockfile = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    deps = lockfile.get("dependencies") or []
    return next(dep for dep in deps if dep.get("repo_url") == "acme/fixture-pkg")


def _write_virtual_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text(
        "# Project\n",
        encoding="utf-8",
    )
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "virtual-hash-roundtrip",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": [_VIRTUAL_DEPENDENCY], "mcp": []},
            }
        ),
        encoding="utf-8",
    )


def _locked_virtual_dep(project: Path) -> dict:
    lockfile = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    deps = lockfile.get("dependencies") or []
    return next(
        dep
        for dep in deps
        if dep.get("repo_url") == "acme/fixture"
        and dep.get("virtual_path") == "instructions/fixture.instructions.md"
    )


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


def test_virtual_lock_replays_across_synthetic_manifest_newline_domains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock made in an LF domain installs and audits in a CRLF domain."""
    project = tmp_path / "virtual-project"
    _write_virtual_project(project)
    newline_domain = {"value": "lf"}
    original_write_text = Path.write_text

    def write_with_platform_newlines(
        path,
        data,
        encoding=None,
        errors=None,
        newline=None,
    ):
        if path.name == "apm.yml" and "apm_modules" in path.parts:
            canonical = data.replace("\r\n", "\n")
            data = (
                canonical.replace("\n", "\r\n") if newline_domain["value"] == "crlf" else canonical
            )
            newline = ""
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    from apm_cli.deps import github_downloader as _ghd

    monkeypatch.setattr(Path, "write_text", write_with_platform_newlines)
    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader,
        "validate_virtual_package_exists",
        lambda self, *args, **kwargs: True,
    )
    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader,
        "_resolve_commit_sha_for_ref",
        lambda self, dep_ref, ref: _VIRTUAL_COMMIT,
    )
    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader,
        "download_raw_file",
        lambda self, dep_ref, file_path, ref: _VIRTUAL_SOURCE,
    )

    runner = CliRunner()
    locked = _run_command(
        runner,
        project,
        monkeypatch,
        [
            "lock",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ],
    )
    assert locked.exit_code == 0, locked.output
    lf_locked_hash = _locked_virtual_dep(project)["content_hash"]

    dep_ref = DependencyReference.parse(_VIRTUAL_DEPENDENCY)
    install_path = dep_ref.get_install_path(project / "apm_modules")
    assert install_path.is_dir()
    assert b"\r\n" not in (install_path / "apm.yml").read_bytes()

    shutil.rmtree(install_path)
    newline_domain["value"] = "crlf"
    installed = _run_command(
        runner,
        project,
        monkeypatch,
        [
            "install",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ],
    )
    assert installed.exit_code == 0, installed.output

    installed_manifest = (install_path / "apm.yml").read_bytes()
    assert b"\r\n" not in installed_manifest
    converged_hash = _locked_virtual_dep(project)["content_hash"]
    assert converged_hash == lf_locked_hash
    assert compute_package_hash(install_path) == converged_hash

    audited = _run_command(
        runner,
        project,
        monkeypatch,
        ["audit", "--ci", "--no-policy", "--format", "json"],
    )
    assert audited.exit_code == 0, audited.output
    json_start = audited.output.find("{")
    assert json_start >= 0, audited.output
    audit_payload = json.loads(audited.output[json_start:])
    assert audit_payload["passed"] is True
    checks = {check["name"]: check for check in audit_payload["checks"]}
    assert checks["content-integrity"]["passed"] is True
