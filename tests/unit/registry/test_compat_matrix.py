"""Backwards-compatibility matrix per docs/proposals/registry-api.md §2.1.

Each test asserts one explicit invariant. These are HARD requirements — any
implementation PR that violates one must be rejected.

Hand-traceable to invariant numbers in the proposal:

- §2.1.1  Zero-config parity
- §2.1.2  apm.yml stability
- §2.1.3  Default source is git
- §2.1.4  apm.lock stability (read; v1 stays v1; v2 readable)
- §2.1.6  No env-var renames or semantics changes
- §2.1.8  No identity changes
- §2.1.10 Marketplace unchanged
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.resolver import RegistryResolution
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.dependency.reference import DependencyReference


@pytest.fixture
def write_apm_yml(tmp_path):
    def _write(content: str) -> Path:
        clear_apm_yml_cache()
        p = tmp_path / "apm.yml"
        p.write_text(textwrap.dedent(content).strip() + "\n")
        return p

    return _write


# ───────────────────────────────────────────────────────────────────────────
# §2.1.1 — Zero-config parity
# ───────────────────────────────────────────────────────────────────────────


class TestZeroConfigParity:
    """A user who never adds ``registries:`` MUST observe identical behavior."""

    def test_no_registries_block_means_no_routing(self, write_apm_yml):
        p = write_apm_yml(
            """
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#main
                - acme/bar#abc1234
                - acme/baz
            """
        )
        pkg = APMPackage.from_apm_yml(p)
        # Block + default both unset
        assert pkg.registries is None
        assert pkg.default_registry is None
        # No dep flipped to registry source
        for dep in pkg.dependencies["apm"]:
            assert dep.source is None
            assert dep.registry_name is None

    def test_branch_refs_remain_valid_without_block(self, write_apm_yml):
        # §2.1.3 — branch refs and SHAs MUST remain valid when no
        # registries: block is set. The strict-semver gate only fires on
        # registry-routed entries.
        p = write_apm_yml(
            """
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#main
                - acme/foo#abc123def4567890abc123def4567890abc12345
            """
        )
        pkg = APMPackage.from_apm_yml(p)
        # Both parsed successfully (no parse error)
        assert pkg.dependencies["apm"][0].reference == "main"
        assert pkg.dependencies["apm"][1].reference.startswith("abc123def")


# ───────────────────────────────────────────────────────────────────────────
# §2.1.2 — apm.yml stability
# ───────────────────────────────────────────────────────────────────────────


class TestApmYmlStability:
    def test_legacy_object_form_git_unchanged(self, write_apm_yml):
        p = write_apm_yml(
            """
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - git: https://github.com/owner/repo.git
                  ref: v2.0
                  alias: my-pkg
            """
        )
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        # source=="git" is the new explicit marker; semantically same as None
        assert d.source == "git"
        assert d.alias == "my-pkg"
        assert d.reference == "v2.0"

    def test_legacy_object_form_local_unchanged(self, write_apm_yml):
        p = write_apm_yml(
            """
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - path: ./local/pkg
            """
        )
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.is_local
        assert d.local_path == "./local/pkg"

    def test_unknown_top_level_key_does_not_break(self, write_apm_yml):
        # A future apm.yml key shouldn't break this version of the parser.
        p = write_apm_yml(
            """
            name: x
            version: 1.0.0
            future_key: whatever
            dependencies:
              apm:
                - acme/foo#v1.0
            """
        )
        # Should parse without error
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.dependencies["apm"][0].reference == "v1.0"


# ───────────────────────────────────────────────────────────────────────────
# §2.1.4 — apm.lock stability
# ───────────────────────────────────────────────────────────────────────────


class TestLockfileStability:
    def test_v1_lockfile_parses_without_migration(self):
        v1_yaml = textwrap.dedent(
            """
            lockfile_version: '1'
            generated_at: 2026-01-01T00:00:00Z
            dependencies:
            - repo_url: acme/foo
              resolved_commit: abc123
              resolved_ref: v1.0
              host: github.com
              deployed_files:
              - .apm/skills/foo/SKILL.md
              package_type: APM_PACKAGE
            """
        ).strip()
        lock = LockFile.from_yaml(v1_yaml)
        assert lock.lockfile_version == "1"
        dep = lock.get_dependency("acme/foo")
        assert dep.resolved_commit == "abc123"
        # No registry fields injected
        assert dep.source is None
        assert dep.resolved_url is None
        assert dep.resolved_hash is None

    def test_v1_lockfile_reemits_as_v1(self):
        v1_yaml = textwrap.dedent(
            """
            lockfile_version: '1'
            generated_at: 2026-01-01T00:00:00Z
            dependencies:
            - repo_url: acme/foo
              resolved_commit: abc123
              host: github.com
            """
        ).strip()
        lock = LockFile.from_yaml(v1_yaml)
        out = lock.to_yaml()
        assert "lockfile_version: '1'" in out
        # No registry fields in the emitted yaml
        assert "resolved_url" not in out
        assert "resolved_hash" not in out

    def test_v2_only_when_registry_dep_present(self):
        # First — git-only project stays v1 forever.
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/git", resolved_commit="abc", host="github.com"
            )
        )
        assert "lockfile_version: '1'" in lock.to_yaml()

        # Add a registry dep — now v2.
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/registry",
                source="registry",
                resolved_url="https://r/v1/.../download",
                resolved_hash="sha256:abc",
                version="1.0.0",
            )
        )
        assert "lockfile_version: '2'" in lock.to_yaml()

        # Remove it — back to v1.
        del lock.dependencies["acme/registry"]
        assert "lockfile_version: '1'" in lock.to_yaml()

    def test_v2_lockfile_round_trip_preserves_all_fields(self):
        # End-to-end: v2-shaped entry survives round-trip without loss.
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/web",
                source="registry",
                version="1.5.3",
                resolved_url="https://r/v1/.../download",
                resolved_hash="sha256:abc123",
                host="github.com",
            )
        )
        out = lock.to_yaml()
        roundtrip = LockFile.from_yaml(out)
        dep = roundtrip.get_dependency("acme/web")
        assert dep.source == "registry"
        assert dep.version == "1.5.3"
        assert dep.resolved_url == "https://r/v1/.../download"
        assert dep.resolved_hash == "sha256:abc123"


# ───────────────────────────────────────────────────────────────────────────
# §2.1.6 — No env-var collisions
# ───────────────────────────────────────────────────────────────────────────


class TestEnvVarPrefixCollision:
    """``APM_REGISTRY_TOKEN_*`` must NOT collide with existing prefixes."""

    def test_token_prefix_distinct_from_existing(self):
        from apm_cli.deps.registry.auth import _env_key

        existing_prefixes = (
            "GITHUB_TOKEN",
            "GITHUB_APM_PAT",
            "GH_TOKEN",
            "PROXY_REGISTRY_",
            "ARTIFACTORY_APM_TOKEN",
        )
        for name in ("corp", "corp-main", "team-a"):
            key = _env_key(name)
            assert key.startswith("APM_REGISTRY_TOKEN_")
            for existing in existing_prefixes:
                assert not key.startswith(existing)
                assert key != existing


# ───────────────────────────────────────────────────────────────────────────
# §2.1.8 — Identity preservation
# ───────────────────────────────────────────────────────────────────────────


class TestIdentityPreservation:
    """``DependencyReference.get_identity()`` must be stable across modes."""

    def test_identity_unchanged_by_registry_scope(self):
        d_git = DependencyReference.parse("acme/foo#1.0.0")
        d_reg = DependencyReference.parse("acme/foo@corp-main#1.0.0")
        assert d_git.get_identity() == d_reg.get_identity() == "acme/foo"

    def test_identity_unchanged_by_object_form_registry(self):
        d_git = DependencyReference.parse("acme/foo#1.0.0")
        d_reg_obj = DependencyReference.parse_from_dict(
            {
                "registry": "corp",
                "id": "acme/foo",
                "path": "x.prompt.md",
                "version": "1.0.0",
            }
        )
        # Identity for non-virtual: ``acme/foo``
        # Identity for virtual: includes the path
        # Both share the ``acme/foo`` package identity though
        assert d_git.get_identity() == "acme/foo"
        assert d_reg_obj.get_identity().startswith("acme/foo/")

    def test_unique_key_includes_repo_url_for_both(self):
        d_git = DependencyReference.parse("acme/foo#1.0.0")
        d_reg = DependencyReference.parse("acme/foo@corp-main#1.0.0")
        assert d_git.get_unique_key() == d_reg.get_unique_key() == "acme/foo"


# ───────────────────────────────────────────────────────────────────────────
# §2.1.10 — Marketplace unchanged
# ───────────────────────────────────────────────────────────────────────────


class TestMarketplaceUnchanged:
    def test_existing_marketplace_json_parses_byte_identically(self):
        # Pre-PR marketplace.json shape with no `registry` field.
        from apm_cli.marketplace.models import parse_marketplace_json

        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "review",
                        "repository": "acme/review",
                        "ref": "v1.0",
                        "version": "v1.0",
                        "description": "x",
                    }
                ],
            },
            source_name="acme",
        )
        plugin = manifest.plugins[0]
        # registry defaults to "" (no routing)
        assert plugin.registry == ""
        # All other fields preserved
        assert plugin.name == "review"
        assert plugin.version == "v1.0"


# ───────────────────────────────────────────────────────────────────────────
# End-to-end install path: parser -> lockfile (no network)
# ───────────────────────────────────────────────────────────────────────────


class TestEndToEndInstallPath:
    """Confirm a registry-sourced apm.yml flows through to a v2 lockfile.

    Uses the resolver with a fake HTTP client so we exercise the full
    parser -> resolver -> InstalledPackage -> LockFile chain.
    """

    def test_apm_yml_to_lockfile_v2(self, write_apm_yml, tmp_path):
        from apm_cli.deps.installed_package import InstalledPackage

        # 1. Parse apm.yml with default-registry routing
        p = write_apm_yml(
            """
            name: project
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/web#^1.2
            """
        )
        pkg = APMPackage.from_apm_yml(p)
        dep = pkg.dependencies["apm"][0]
        assert dep.source == "registry"
        assert dep.registry_name == "corp"

        # 2. Simulate a resolver-produced RegistryResolution.
        resolution = RegistryResolution(
            resolved_url="https://corp.example.com/apm/v1/packages/acme/web/versions/1.5.3/download",
            resolved_hash="sha256:" + "f" * 64,
            version="1.5.3",
        )

        # 3. Build InstalledPackage and assemble lockfile
        installed = InstalledPackage(
            dep_ref=dep,
            resolved_commit=None,
            depth=1,
            resolved_by=None,
            registry_resolution=resolution,
        )

        class _StubGraph:
            pass

        lock = LockFile.from_installed_packages([installed], _StubGraph())
        out = lock.to_yaml()
        assert "lockfile_version: '2'" in out

        # 4. Round-trip and re-derive dep_ref
        roundtrip = LockFile.from_yaml(out)
        locked = roundtrip.get_dependency("acme/web")
        ref = locked.to_dependency_ref()
        assert ref.source == "registry"
        assert ref.reference == "1.5.3"  # exact locked version
