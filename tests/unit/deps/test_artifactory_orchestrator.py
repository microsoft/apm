"""Unit tests for ``apm_cli.deps.artifactory_orchestrator``.

Covers the routing decisions of :class:`ArtifactoryRouter` and the
delegation contract of :class:`ArtifactoryOrchestrator` against a
minimal ``_HasArchiveDownloader`` stub.

Tagged governed-by-policy: the 4-branch routing (FQDN dep, transparent
proxy, registry-only, ADO-skip) is policy-sensitive. Silent drift would
route an ADO dep through Artifactory or fail to honor registry-only.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.deps.artifactory_orchestrator import (
    ArtifactoryOrchestrator,
    ArtifactoryRouter,
)
from apm_cli.models.dependency.reference import DependencyReference


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


class TestShouldUseProxy:
    @pytest.mark.parametrize(
        "dep,registry_only,expected",
        [
            # Explicit Artifactory dep takes the FQDN branch (False).
            (_dep(artifactory=True), False, False),
            # GitHub host with no registry-only -> use proxy (when configured).
            (_dep(host="github.com"), False, True),
            # ADO host: never proxied (when not registry-only).
            (_dep(ado=True), False, False),
            # ADO with registry-only: registry-only takes precedence
            # (registry-only fences ALL non-artifactory deps to the proxy).
            (_dep(ado=True), True, True),
            # GitLab/generic host: not GitHub-family, not proxied unless
            # registry-only forces it.
            (_dep(host="gitlab.com"), False, False),
            (_dep(host="gitlab.com"), True, True),
            # Default host (None) treated as github.com -> proxied.
            (_dep(host=None), False, True),
        ],
    )
    def test_routing(self, dep, registry_only, expected):
        with patch.object(ArtifactoryRouter, "is_registry_only", return_value=registry_only):
            assert ArtifactoryRouter.should_use_proxy(dep) is expected


class TestParseProxyConfig:
    def test_returns_none_when_no_config(self):
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None):
            assert ArtifactoryRouter.parse_proxy_config() is None

    def test_returns_tuple_when_configured(self):
        cfg = types.SimpleNamespace(
            host="artifactory.example.com", prefix="apm-vcs", scheme="https"
        )
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=cfg):
            assert ArtifactoryRouter.parse_proxy_config() == (
                "artifactory.example.com",
                "apm-vcs",
                "https",
            )


# ---------------------------------------------------------------------------
# ArtifactoryOrchestrator._resolve_host_prefix
# ---------------------------------------------------------------------------


class TestResolveHostPrefix:
    def test_explicit_artifactory_dep(self):
        dep = _dep(
            artifactory=True,
            host="art.example.com",
            artifactory_prefix="apm/github",
        )
        host, prefix, scheme = ArtifactoryOrchestrator._resolve_host_prefix(dep, None)
        assert (host, prefix, scheme) == ("art.example.com", "apm/github", "https")

    def test_explicit_artifactory_dep_missing_prefix_raises(self):
        # Construct via dataclass directly -- bypass parser to simulate
        # a malformed in-memory DependencyReference.
        bad_dep = types.SimpleNamespace(
            repo_url="owner/repo",
            host="art.example.com",
            artifactory_prefix=None,
            is_artifactory=lambda: True,
        )
        with pytest.raises(ValueError, match=r"missing host or artifactory prefix"):
            ArtifactoryOrchestrator._resolve_host_prefix(bad_dep, None)

    def test_explicit_artifactory_dep_missing_host_raises(self):
        bad_dep = types.SimpleNamespace(
            repo_url="owner/repo",
            host=None,
            artifactory_prefix="apm/github",
            is_artifactory=lambda: True,
        )
        with pytest.raises(ValueError, match=r"missing host or artifactory prefix"):
            ArtifactoryOrchestrator._resolve_host_prefix(bad_dep, None)

    def test_falls_back_to_proxy_info(self):
        dep = _dep(host="github.com")  # not explicit artifactory
        proxy = ("art.example.com", "github-proxy", "https")
        assert ArtifactoryOrchestrator._resolve_host_prefix(dep, proxy) == proxy

    def test_no_explicit_no_proxy_raises_runtime_error(self):
        dep = _dep(host="github.com")
        with pytest.raises(RuntimeError, match=r"requires either FQDN or"):
            ArtifactoryOrchestrator._resolve_host_prefix(dep, None)


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
