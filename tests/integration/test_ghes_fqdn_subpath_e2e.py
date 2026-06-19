"""End-to-end parse tests for GHES FQDN monorepo subpath shorthand (#1673).

The fix in ``is_github_hostname()`` lets FQDN shorthand strings like
``ghe.example.com/org/repo/packages/skill`` split at the repo boundary
(``git: org/repo`` + ``path: packages/skill``) instead of embedding the
whole path into the clone URL -- but only when ``GITHUB_HOST`` names the
host as a GitHub Enterprise Server.

The 17 unit tests in ``tests/unit/test_generic_git_urls.py`` and
``tests/unit/test_github_host.py`` cover the parse boundary directly via
``DependencyReference.parse``. These tests pin the *end-to-end* contract
through the real install entry point ``APMPackage.from_apm_yml`` -- the
codepath a user actually exercises with ``apm install`` -- so a future
refactor that regresses host classification is caught by an automated
test rather than surfacing as a silent clone failure in production.

Hermetic: no network, no marker required (parses a manifest on disk).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _write_apm_yml(path: Path, deps: list) -> Path:
    apm_yml = path / "apm.yml"
    config = {"name": "consumer-pkg", "version": "1.0.0", "dependencies": {"apm": deps}}
    apm_yml.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    return apm_yml


class TestGHESFQDNSubpathThroughManifest:
    """#1673: GHES FQDN subpath splits correctly through APMPackage.from_apm_yml."""

    def test_ghes_subpath_splits_through_from_apm_yml(self, tmp_path, monkeypatch):
        """With GITHUB_HOST set, the dep parses into repo_url + virtual_path."""
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        apm_yml = _write_apm_yml(tmp_path, ["ghe.example.com/org/repo/packages/skill"])

        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()

        assert len(deps) == 1
        dep = deps[0]
        assert dep.host == "ghe.example.com"
        assert dep.repo_url == "org/repo"
        assert dep.virtual_path == "packages/skill"
        assert dep.is_virtual is True

    def test_ghes_subpath_with_ref_through_from_apm_yml(self, tmp_path, monkeypatch):
        """A pinned ref is preserved alongside the subpath split."""
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        apm_yml = _write_apm_yml(tmp_path, ["ghe.example.com/org/repo/packages/skill#v1.0.0"])

        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()

        assert len(deps) == 1
        dep = deps[0]
        assert dep.repo_url == "org/repo"
        assert dep.virtual_path == "packages/skill"
        assert dep.reference == "v1.0.0"

    def test_without_github_host_subpath_is_not_split(self, tmp_path, monkeypatch):
        """Recovery-path contract: unset GITHUB_HOST -> generic host, no split.

        This is the pre-fix behaviour and documents why GITHUB_HOST is
        required: the subpath stays embedded in repo_url rather than being
        promoted to a virtual path. Pinning it guards against an accidental
        widening of GHES detection to hosts that were never configured.
        """
        monkeypatch.delenv("GITHUB_HOST", raising=False)
        apm_yml = _write_apm_yml(tmp_path, ["ghe.example.com/org/repo/packages/skill"])

        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()

        assert len(deps) == 1
        dep = deps[0]
        assert dep.virtual_path != "packages/skill"
        assert dep.repo_url != "org/repo"
