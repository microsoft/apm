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

pytestmark = [pytest.mark.component]

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


class _NestedSkillDownloader(_ContentHashOnlyDownloader):
    """Materialize a selected nested skill plus a source-only fixture."""

    def __init__(self, *, hostile_skill: bool) -> None:
        super().__init__()
        self.hostile_skill = hostile_skill

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
                    "name": "nested-skill-fixture",
                    "version": "1.0.0",
                    "description": "nested skill security fixture",
                }
            ),
            encoding="utf-8",
        )
        skill = target_path / "skills" / "clean" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "clean \u202e skill\n" if self.hostile_skill else "clean skill\n",
            encoding="utf-8",
        )
        source_only = target_path / "src" / "fixture.txt"
        source_only.parent.mkdir()
        source_only.write_text("source-only \u202e fixture\n", encoding="utf-8")
        package = APMPackage.from_apm_yml(target_path / "apm.yml")
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            resolved_reference=ResolvedReference(
                original_ref="default",
                ref_type=GitReferenceType.BRANCH,
                resolved_commit=_VIRTUAL_COMMIT,
                ref_name="default",
            ),
        )


def _write_project(project: Path, *, target: str = "copilot") -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text("# Project\n", encoding="utf-8")
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "content-hash-roundtrip",
                "version": "1.0.0",
                "target": target,
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


@pytest.mark.lifecycle_smoke
def test_source_only_hidden_character_allows_skill_install_and_records_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source fixture cannot block or become part of a selected skill deploy."""
    project = tmp_path / "source-only"
    _write_project(project)
    downloader = _NestedSkillDownloader(hostile_skill=False)

    from apm_cli.deps import github_downloader as _ghd

    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader, "download_package", downloader.download_package
    )
    result = _run_install(CliRunner(), project, monkeypatch)

    assert result.exit_code == 0, result.output
    deployed = project / ".agents" / "skills" / "clean" / "SKILL.md"
    assert deployed.read_text(encoding="utf-8") == "clean skill\n"
    assert not (project / ".agents" / "skills" / "clean" / "src" / "fixture.txt").exists()
    locked = _locked_dep(project)
    assert ".agents/skills/clean/SKILL.md" in locked["deployed_files"]


@pytest.mark.lifecycle_smoke
def test_hidden_character_in_selected_skill_blocks_without_deployment_or_lock_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployable hidden character fails closed before target or lock writes."""
    project = tmp_path / "hostile-skill"
    _write_project(project)
    before_manifest = (project / "apm.yml").read_bytes()
    downloader = _NestedSkillDownloader(hostile_skill=True)

    from apm_cli.deps import github_downloader as _ghd

    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader, "download_package", downloader.download_package
    )
    result = _run_install(CliRunner(), project, monkeypatch)

    assert result.exit_code != 0
    assert not (project / ".agents" / "skills" / "clean").exists()
    assert not (project / "apm.lock.yaml").exists()
    assert (project / "apm.yml").read_bytes() == before_manifest
    assert "Fix the reported file(s) in the package source, then reinstall" in result.output


@pytest.mark.lifecycle_smoke
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


def test_project_install_with_dependency_instructions_prints_compile_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary dependency installs surface the post-install compile step."""
    project = tmp_path / "project"
    _write_project(project, target="gemini")
    downloader = _ContentHashOnlyDownloader()

    from apm_cli.deps import github_downloader as _ghd

    monkeypatch.setattr(
        _ghd.GitHubPackageDownloader, "download_package", downloader.download_package
    )

    result = _run_install(CliRunner(), project, monkeypatch)

    assert result.exit_code == 0, result.output
    normalized_output = " ".join(result.output.split())
    assert (
        "Instructions installed for gemini. Run 'apm compile' to update AGENTS.md / GEMINI.md."
    ) in normalized_output


@pytest.mark.lifecycle_smoke
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


@pytest.mark.windows_compat
def test_marketplace_plugin_synthetic_manifest_hash_is_newline_invariant(
    tmp_path: Path,
) -> None:
    """apm#2619: the synthesize + stamp chain must yield LF manifests.

    Marketplace-plugin downloads (both ``download_package`` and
    ``download_subdirectory_package``) run ``validate_apm_package`` --
    which synthesizes ``apm.yml`` and serializes inline hooks to
    ``.apm/hooks/hooks.json`` -- and then ``stamp_plugin_version`` --
    which rewrites ``apm.yml`` with the short commit SHA. All of these
    writes land inside the tree ``compute_package_hash`` hashes raw, so
    platform-native line endings made the lockfile ``content_hash`` differ
    between Windows (CRLF) and POSIX (LF) for byte-identical upstream
    content.

    The fixture is imported from the CRLF-invariance probe so the 3-OS CI
    workflow and this test always exercise the same tree shape.
    """
    from apm_cli.deps.package_validator import stamp_plugin_version
    from apm_cli.models.validation import PackageType, validate_apm_package
    from scripts.crlf_invariance_probe import PROBE_STAMP_SHA, build_synthetic_plugin_fixture

    pkg = tmp_path / "pkg"
    build_synthetic_plugin_fixture(pkg)

    result = validate_apm_package(pkg)
    assert result.is_valid, result.errors
    assert result.package_type == PackageType.MARKETPLACE_PLUGIN
    stamp_plugin_version(
        result.package,
        result.package_type,
        PROBE_STAMP_SHA,
        pkg,
    )
    assert result.package.version == PROBE_STAMP_SHA[:7]

    manifest = (pkg / "apm.yml").read_bytes()
    assert b"\r" not in manifest  # LF-deterministic on every OS
    hooks_json = (pkg / ".apm" / "hooks" / "hooks.json").read_bytes()
    assert b"\r" not in hooks_json  # inline hooks writer, second fix site

    lf_hash = compute_package_hash(pkg)
    # The manifest is hash-visible: CRLF-ifying it changes the package
    # hash. This is exactly why the production writers must emit LF bytes.
    (pkg / "apm.yml").write_bytes(manifest.replace(b"\n", b"\r\n"))
    assert compute_package_hash(pkg) != lf_hash
