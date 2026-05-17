"""Tests for the top-level ``registries:`` block in apm.yml.

Covers ``APMPackage.from_apm_yml`` parsing of the new block plus the
default-registry routing pass per docs/proposals/registry-api.md §3.1/§3.2.

The hardest invariant: a project without a ``registries:`` block is
byte-identical to pre-PR behavior (invariant §2.1.1).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


@pytest.fixture
def write_yml(tmp_path):
    """Yield a helper that writes ``apm.yml`` content to a temp dir."""

    def _write(content: str) -> Path:
        clear_apm_yml_cache()
        p = tmp_path / "apm.yml"
        p.write_text(textwrap.dedent(content).strip() + "\n")
        return p

    return _write


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    """Most tests in this module exercise the experimental registry feature."""
    import apm_cli.config as _conf

    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {"experimental": {"package_registry": True}},
    )


def _disable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    clear_apm_yml_cache()
    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {}})


# ───────────────────────────────────────────────────────────────────────────
# Block parsing
# ───────────────────────────────────────────────────────────────────────────


class TestRegistriesBlockParsing:
    def test_no_block_means_no_change(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#main
                - acme/bar#v1.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries is None
        assert pkg.default_registry is None
        for dep in pkg.dependencies["apm"]:
            assert dep.source is None

    def test_block_without_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-main:
                url: https://corp.example.com/apm
            dependencies:
              apm:
                - acme/foo#v1.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries == {"corp-main": "https://corp.example.com/apm"}
        assert pkg.default_registry is None
        # Without a default, plain shorthand stays on Git.
        assert pkg.dependencies["apm"][0].source is None

    def test_block_with_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.default_registry == "corp"
        assert "corp" in pkg.registries

    def test_multiple_registries(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-a:
                url: https://a.example.com/apm
              corp-b:
                url: https://b.example.com/apm
              default: corp-a
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert set(pkg.registries.keys()) == {"corp-a", "corp-b"}
        assert pkg.default_registry == "corp-a"

    def test_empty_block(self, write_yml):
        # ``registries: {}`` is harmless and yields no fields set.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries: {}
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries is None
        assert pkg.default_registry is None


class TestPackageRegistryExperimentalGate:
    def test_non_empty_registries_block_requires_flag(self, write_yml, monkeypatch):
        _disable_package_registry(monkeypatch)
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
            """)
        with pytest.raises(
            ValueError, match="apm experimental enable package-registry"
        ):
            APMPackage.from_apm_yml(p)

    def test_empty_registries_block_allowed_without_flag(self, write_yml, monkeypatch):
        _disable_package_registry(monkeypatch)
        p = write_yml("""
            name: x
            version: 1.0.0
            registries: {}
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries is None
        assert pkg.default_registry is None


class TestRegistriesBlockValidation:
    def test_non_mapping_block_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries: not-a-mapping
            """)
        with pytest.raises(ValueError, match="must be a mapping"):
            APMPackage.from_apm_yml(p)

    def test_missing_url_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp: {}
            """)
        with pytest.raises(ValueError, match="missing required field 'url:'"):
            APMPackage.from_apm_yml(p)

    def test_non_http_url_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: ftp://corp.example.com
            """)
        with pytest.raises(ValueError, match="https:// or http://"):
            APMPackage.from_apm_yml(p)

    def test_unknown_field_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
                token: oops-not-here
            """)
        with pytest.raises(ValueError, match="unknown fields"):
            APMPackage.from_apm_yml(p)

    def test_unknown_default_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com
              default: nonexistent
            """)
        with pytest.raises(ValueError, match="unconfigured registry"):
            APMPackage.from_apm_yml(p)


# ───────────────────────────────────────────────────────────────────────────
# Default-registry routing pass
# ───────────────────────────────────────────────────────────────────────────


class TestDefaultRouting:
    def test_shorthand_routes_to_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#^1.0.0
                - acme/bar#1.2.3
            """)
        pkg = APMPackage.from_apm_yml(p)
        for dep in pkg.dependencies["apm"]:
            assert dep.source == "registry"
            assert dep.registry_name == "corp"

    def test_at_scope_overrides_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-main:
                url: https://main.example.com/apm
              corp-other:
                url: https://other.example.com/apm
              default: corp-main
            dependencies:
              apm:
                - acme/foo@corp-other#^1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.registry_name == "corp-other"

    def test_explicit_git_object_form_not_routed(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - git: https://github.com/owner/repo.git
                  ref: v2.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.source == "git"  # explicit, not "registry"

    def test_object_form_registry_preserved(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-main:
                url: https://main.example.com/apm
              corp-other:
                url: https://other.example.com/apm
              default: corp-main
            dependencies:
              apm:
                - registry: corp-other
                  id: acme/prompts
                  path: a/b.prompt.md
                  version: 1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.registry_name == "corp-other"

    def test_local_path_not_routed(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - ./local-pkg
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.is_local
        assert d.source is None

    def test_dev_dependencies_routed_too(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            devDependencies:
              apm:
                - acme/foo#1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dev_dependencies["apm"][0]
        assert d.source == "registry"
        assert d.registry_name == "corp"


class TestDefaultRoutingErrors:
    def test_branch_ref_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#main
            """)
        with pytest.raises(ValueError, match="not a semver"):
            APMPackage.from_apm_yml(p)

    def test_commit_sha_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#abc123d
            """)
        with pytest.raises(ValueError, match="not a semver"):
            APMPackage.from_apm_yml(p)

    def test_missing_ref_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo
            """)
        with pytest.raises(ValueError, match="no version constraint"):
            APMPackage.from_apm_yml(p)

    def test_remediation_mentions_git_alternative(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#develop
            """)
        with pytest.raises(ValueError) as excinfo:
            APMPackage.from_apm_yml(p)
        assert "- git:" in str(excinfo.value)


# ───────────────────────────────────────────────────────────────────────────
# Backwards-compatibility invariants
# ───────────────────────────────────────────────────────────────────────────


class TestBackwardsCompatibility:
    """A project without ``registries:`` MUST behave byte-identically to pre-PR."""

    def test_branch_ref_still_valid_without_registries(self, write_yml):
        # Per invariant §2.1.3, branch refs remain valid in the absence of
        # a registries: block — the parser stays ref-opaque on the Git path.
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#main
                - acme/foo#abc123d
            """)
        pkg = APMPackage.from_apm_yml(p)
        for dep in pkg.dependencies["apm"]:
            assert dep.source is None

    def test_no_version_still_valid_without_registries(self, write_yml):
        # `acme/foo` without a #ref is fine when no default registry is set.
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.dependencies["apm"][0].reference is None
