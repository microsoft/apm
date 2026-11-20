"""ArtifactoryOrchestrator package and subdirectory downloads into an install tree."""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.deps.artifactory_orchestrator import (
    ArtifactoryOrchestrator,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component


def _dep(
    *,
    host: str | None = None,
    repo_url: str = "owner/repo",
    artifactory: bool = False,
    artifactory_prefix: str | None = None,
    ado: bool = False,
    reference: str | None = None,
):
    kwargs: dict = {"repo_url": repo_url, "host": host, "reference": reference}
    if artifactory:
        kwargs["artifactory_prefix"] = artifactory_prefix or "artifactory/github"
        kwargs["host"] = host or "artifactory.example.com"
    if ado:
        kwargs.update(
            host=host or "dev.azure.com",
            ado_organization="myorg",
            ado_project="myproj",
            ado_repo="myrepo",
        )
    return DependencyReference(**kwargs)


# ---------------------------------------------------------------------------
# ArtifactoryRouter.should_use_proxy
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ArtifactoryOrchestrator._resolve_host_prefix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ArtifactoryOrchestrator.download_package delegation
# ---------------------------------------------------------------------------


class TestDownloadPackageDelegation:
    def _orchestrator_with_validation_ok(self):
        """Build an orchestrator wired to a fake archive_downloader.

        Patches validate_apm_package to succeed without writing real files.
        """
        archive_downloader = MagicMock()
        return ArtifactoryOrchestrator(archive_downloader), archive_downloader

    def test_delegates_to_archive_downloader_with_resolved_host_prefix(self, tmp_path):
        orch, archive = self._orchestrator_with_validation_ok()
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
            repo_url="owner/repo",
            reference="v1.0.0",
        )
        target = tmp_path / "pkg"

        valid_pkg = types.SimpleNamespace(source=None, resolved_commit=None, name="pkg")
        validation = types.SimpleNamespace(
            is_valid=True,
            errors=[],
            package=valid_pkg,
            package_type="apm",
        )
        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            result = orch.download_package(dep, target)

        archive.download_artifactory_archive.assert_called_once()
        call = archive.download_artifactory_archive.call_args
        assert call.args[:5] == ("art.example.com", "apm/github", "owner", "repo", "v1.0.0")
        assert call.args[5] == target
        assert call.kwargs["scheme"] == "https"
        assert result.dependency_ref is dep
        assert result.install_path == target

    def test_whole_repo_download_carries_root_licence_next_to_skills(self, tmp_path):
        """#3556: an Artifactory whole-repo skill below the root gets the root licence."""
        orch, archive = self._orchestrator_with_validation_ok()
        dep = _dep(artifactory=True, host="art.example.com", repo_url="owner/repo")
        target = tmp_path / "pkg"

        def materialize(*args, **kwargs):
            del args, kwargs
            (target / "skills" / "alpha").mkdir(parents=True)
            (target / "skills" / "alpha" / "SKILL.md").write_text("# a\n", encoding="utf-8")
            (target / "LICENSE").write_bytes(b"MIT License\n")

        archive.download_artifactory_archive.side_effect = materialize
        validation = types.SimpleNamespace(
            is_valid=True,
            errors=[],
            package=types.SimpleNamespace(source=None, resolved_commit=None, name="pkg"),
            package_type="apm",
        )
        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            result = orch.download_package(dep, target)

        assert (target / "skills" / "alpha" / "LICENSE").read_bytes() == b"MIT License\n"
        assert [(r.path, r.source) for r in result.carried_licenses] == [
            ("skills/alpha/LICENSE", "LICENSE")
        ]

    def test_default_ref_is_main_when_none(self, tmp_path):
        orch, archive = self._orchestrator_with_validation_ok()
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
            repo_url="owner/repo",
            reference=None,
        )
        valid_pkg = types.SimpleNamespace(source=None, resolved_commit=None, name="pkg")
        validation = types.SimpleNamespace(
            is_valid=True, errors=[], package=valid_pkg, package_type="apm"
        )
        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            orch.download_package(dep, tmp_path / "pkg")
        # The 5th positional arg is `ref`; defaults to "main".
        assert archive.download_artifactory_archive.call_args.args[4] == "main"

    def test_full_sha_preserves_immutable_resolution_metadata(self, tmp_path):
        """A frozen full-SHA replay must retain its lockfile integrity anchor."""
        from apm_cli.models.apm_package import GitReferenceType

        commit = "a" * 40
        orch, archive = self._orchestrator_with_validation_ok()
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
            repo_url="owner/repo",
            reference=commit,
        )
        valid_pkg = types.SimpleNamespace(source=None, resolved_commit=None, name="pkg")
        validation = types.SimpleNamespace(
            is_valid=True, errors=[], package=valid_pkg, package_type="apm"
        )

        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            result = orch.download_package(dep, tmp_path / "pkg")

        assert archive.download_artifactory_archive.call_args.args[4] == commit
        assert result.package.resolved_commit == commit
        assert result.resolved_reference.ref_type is GitReferenceType.COMMIT
        assert result.resolved_reference.resolved_commit == commit

    def test_validation_failure_raises_and_cleans_up(self, tmp_path):
        orch, _archive = self._orchestrator_with_validation_ok()
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
            repo_url="owner/repo",
            reference="main",
        )
        target = tmp_path / "pkg"
        target.mkdir()
        validation = types.SimpleNamespace(
            is_valid=False, errors=["missing apm.yml"], package=None, package_type=None
        )
        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            with pytest.raises(RuntimeError, match=r"Invalid APM package"):
                orch.download_package(dep, target)

    def test_native_agent_plugin_returns_projected_package_without_apm_yml(self, tmp_path):
        from apm_cli.agent_plugins import PLUGIN_SCHEMA_ID
        from apm_cli.models.validation import PackageType

        orch, archive = self._orchestrator_with_validation_ok()
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
            repo_url="owner/native",
            reference="v1.0.0",
        )
        target = tmp_path / "pkg"

        def materialize(*args, **kwargs):
            del args, kwargs
            target.mkdir(exist_ok=True)
            (target / "plugin.json").write_text(
                json.dumps(
                    {
                        "$schema": PLUGIN_SCHEMA_ID,
                        "name": "native.plugin",
                        "version": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )

        archive.download_artifactory_archive.side_effect = materialize

        result = orch.download_package(dep, target)

        assert result.package_type == PackageType.AGENT_PLUGIN
        assert result.package is not None
        assert result.package.name == "native.plugin"
        assert result.package.agent_plugin is not None
        assert not (target / "apm.yml").exists()

    def test_archive_runtime_error_propagates(self, tmp_path):
        orch, archive = self._orchestrator_with_validation_ok()
        archive.download_artifactory_archive.side_effect = RuntimeError("404")
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
            repo_url="owner/repo",
            reference="main",
        )
        with pytest.raises(RuntimeError, match=r"404"):
            orch.download_package(dep, tmp_path / "pkg")


class TestDownloadSubdirectoryResolution:
    """Artifactory virtual packages retain frozen commit metadata."""

    def test_full_sha_preserves_immutable_resolution_metadata(self, tmp_path):
        from apm_cli.models.apm_package import GitReferenceType

        commit = "a" * 40
        archive = MagicMock()
        orch = ArtifactoryOrchestrator(archive)
        dep = DependencyReference(
            repo_url="owner/repo",
            host="art.example.com",
            artifactory_prefix="apm/github",
            reference=commit,
            virtual_path="nested",
        )
        valid_pkg = types.SimpleNamespace(source=None, resolved_commit=None, name="pkg")
        validation = types.SimpleNamespace(
            is_valid=True, errors=[], package=valid_pkg, package_type="apm"
        )

        def materialize(*args, **kwargs):
            del args, kwargs
            archive_target = archive.download_artifactory_archive.call_args.args[5]
            nested = archive_target / "nested"
            nested.mkdir(parents=True)
            (nested / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")

        archive.download_artifactory_archive.side_effect = materialize
        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            result = orch.download_subdirectory(
                dep,
                tmp_path / "pkg",
                ("art.example.com", "apm/github", "https"),
            )

        assert archive.download_artifactory_archive.call_args.args[4] == commit
        assert result.package.resolved_commit == commit
        assert result.resolved_reference.ref_type is GitReferenceType.COMMIT
        assert result.resolved_reference.resolved_commit == commit

    def test_archive_root_licence_is_carried_into_subdirectory_package(self, tmp_path):
        """#3556: the archive root LICENSE travels with an Artifactory subdirectory."""
        archive = MagicMock()
        orch = ArtifactoryOrchestrator(archive)
        dep = DependencyReference(
            repo_url="owner/repo",
            host="art.example.com",
            artifactory_prefix="apm/github",
            reference="main",
            virtual_path="skills/grilling",
        )
        validation = types.SimpleNamespace(
            is_valid=True,
            errors=[],
            package=types.SimpleNamespace(source=None, resolved_commit=None, name="pkg"),
            package_type="apm",
        )

        def materialize(*args, **kwargs):
            del args, kwargs
            root = archive.download_artifactory_archive.call_args.args[5]
            (root / "skills" / "grilling").mkdir(parents=True)
            (root / "skills" / "grilling" / "SKILL.md").write_text("# g\n", encoding="utf-8")
            (root / "LICENSE").write_bytes(b"MIT License\n")

        archive.download_artifactory_archive.side_effect = materialize
        with patch(
            "apm_cli.deps.artifactory_orchestrator.validate_apm_package",
            return_value=validation,
        ):
            result = orch.download_subdirectory(
                dep,
                tmp_path / "pkg",
                ("art.example.com", "apm/github", "https"),
            )

        assert (tmp_path / "pkg" / "LICENSE").read_bytes() == b"MIT License\n"
        assert [(r.path, r.source) for r in result.carried_licenses] == [("LICENSE", "LICENSE")]


# ---------------------------------------------------------------------------
# ArtifactoryOrchestrator._split_owner_repo subgroup folding
# ---------------------------------------------------------------------------
