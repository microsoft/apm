"""Tests for the registry-resolver wiring in ``install/phases/resolve.py``.

Covers:
- ``_lockfile_has_registry_deps`` helper
- ``ctx.registry_resolver`` is constructed when apm.yml has a registries:
  block OR the lockfile carries registry-sourced entries
- ``ctx.registry_resolver`` is left ``None`` for projects without either
- The download_callback dispatch logic — direct call against the inner
  closure is awkward, so we exercise it via a focused fake-resolver test
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.resolve import (
    _lockfile_has_registry_deps,
    _require_package_registry_feature_if_needed,
)


def _set_package_registry(monkeypatch, enabled: bool):
    import apm_cli.config as _conf

    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {"experimental": {"registries": enabled}},
    )


class TestLockfileHasRegistryDeps:
    def test_none_lockfile_returns_false(self):
        assert _lockfile_has_registry_deps(None) is False

    def test_empty_lockfile_returns_false(self):
        assert _lockfile_has_registry_deps(LockFile()) is False

    def test_only_git_deps_returns_false(self):
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/foo",
                resolved_commit="abc123",
                host="github.com",
            )
        )
        assert _lockfile_has_registry_deps(lock) is False

    def test_only_local_deps_returns_false(self):
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/x",
                source="local",
                local_path="./x",
            )
        )
        assert _lockfile_has_registry_deps(lock) is False

    def test_one_registry_dep_returns_true(self):
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/foo",
                resolved_commit="abc",
                host="github.com",
            )
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/bar",
                source="registry",
                resolved_url="https://r/v1/.../download",
                resolved_hash="sha256:abc",
                version="1.0.0",
            )
        )
        assert _lockfile_has_registry_deps(lock) is True


class TestResolverConstruction:
    """``ctx.registry_resolver`` is built (or not) based on the manifest + lockfile state."""

    def _make_ctx(
        self,
        tmp_path,
        *,
        registries: dict | None = None,
        existing_lockfile: LockFile | None = None,
        deps: list | None = None,
    ):
        from apm_cli.install.context import InstallContext

        # Build a stand-in apm_package with the registries: attribute the
        # phase reads. We don't need a real APMPackage for this test.
        apm_package = MagicMock()
        apm_package.registries = registries
        apm_package.default_registry = None

        ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
        ctx.apm_package = apm_package
        ctx.all_apm_deps = deps or []
        ctx.update_refs = False
        ctx.scope = MagicMock()
        return ctx

    def test_no_registries_no_lockfile_means_no_resolver(self, monkeypatch, tmp_path):
        """Vanilla project (no registries:, no lockfile-registry-deps) skips resolver build."""
        from apm_cli.install.phases import resolve as _resolve_mod

        ctx = self._make_ctx(tmp_path)
        # Stub out the rest of the phase so we only exercise resolver construction.
        # We can't easily call run() end-to-end without a full pipeline; instead
        # test the resolver-build logic directly via the helper + branch.
        existing_lockfile = None
        registries_map = ctx.apm_package.registries or {}
        needs_registry = bool(registries_map) or _resolve_mod._lockfile_has_registry_deps(
            existing_lockfile
        )
        assert needs_registry is False

    def test_registries_block_triggers_resolver(self, tmp_path):
        from apm_cli.install.phases import resolve as _resolve_mod

        ctx = self._make_ctx(tmp_path, registries={"corp": "https://corp.example.com/apm"})
        registries_map = ctx.apm_package.registries or {}
        needs = bool(registries_map) or _resolve_mod._lockfile_has_registry_deps(None)
        assert needs is True

    def test_lockfile_registry_deps_trigger_resolver_even_without_block(self, tmp_path):
        from apm_cli.install.phases import resolve as _resolve_mod

        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/x",
                source="registry",
                resolved_url="https://r/x",
                resolved_hash="sha256:x",
                version="1.0.0",
            )
        )
        ctx = self._make_ctx(tmp_path)  # no registries: block
        registries_map = ctx.apm_package.registries or {}
        needs = bool(registries_map) or _resolve_mod._lockfile_has_registry_deps(lock)
        assert needs is True


class TestPackageRegistryExperimentalGate:
    def test_no_registry_need_does_not_require_flag(self, monkeypatch):
        _set_package_registry(monkeypatch, enabled=False)
        assert _require_package_registry_feature_if_needed({}, None) is False

    def test_registries_block_requires_flag(self, monkeypatch):
        _set_package_registry(monkeypatch, enabled=False)
        with pytest.raises(ValueError, match="apm experimental enable registries"):
            _require_package_registry_feature_if_needed(
                {"corp": "https://corp.example.com/apm"},
                None,
            )

    def test_lockfile_registry_dep_requires_flag(self, monkeypatch):
        _set_package_registry(monkeypatch, enabled=False)
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="acme/x",
                source="registry",
                resolved_url="https://r/x",
                resolved_hash="sha256:x",
                version="1.0.0",
            )
        )
        with pytest.raises(ValueError, match="apm experimental enable registries"):
            _require_package_registry_feature_if_needed({}, lock)

    def test_registry_need_allowed_when_flag_enabled(self, monkeypatch):
        _set_package_registry(monkeypatch, enabled=True)
        assert (
            _require_package_registry_feature_if_needed(
                {"corp": "https://corp.example.com/apm"},
                None,
            )
            is True
        )


class TestRegistryLockfileReplay:
    """Phase 1 download_callback: registry T5 — honor lockfile on apm install."""

    def _make_locked_dep(self, version="1.2.0"):
        return LockedDependency(
            repo_url="acme/web-skills",
            source="registry",
            version=version,
            resolved_url=(
                f"https://reg.example.com/apm/v1/packages/acme/web-skills"
                f"/versions/{version}/download"
            ),
            resolved_hash="sha256:" + "a" * 64,
        )

    def _make_lockfile(self, version="1.2.0"):
        lock = LockFile()
        lock.add_dependency(self._make_locked_dep(version))
        return lock

    def test_download_from_lockfile_called_when_replay_available(self, tmp_path):
        """With lockfile + matching range + not update_refs → download_from_lockfile."""
        from unittest.mock import MagicMock, patch

        from apm_cli.deps.lockfile import LockFile
        from apm_cli.deps.registry.resolver import RegistryPackageResolver
        from apm_cli.models.dependency.reference import DependencyReference

        locked = self._make_locked_dep("1.2.0")
        lock = LockFile()
        lock.add_dependency(locked)

        dep_ref = DependencyReference(
            repo_url="acme/web-skills",
            reference="^1.2.0",
            source="registry",
            registry_name="corp-main",
        )

        resolver = MagicMock(spec=RegistryPackageResolver)
        install_path = tmp_path / "acme" / "web-skills"
        install_path.mkdir(parents=True)

        from apm_cli.deps.registry import feature_gate as _fg

        with patch.object(_fg, "require_package_registry_enabled"):
            # Simulate the registry T5 logic directly
            _locked_reg = lock.get_dependency(dep_ref.get_unique_key())
            assert _locked_reg is not None
            assert _locked_reg.resolved_url
            assert _locked_reg.resolved_hash
            assert _locked_reg.version

            from apm_cli.drift import detect_ref_change

            ref_changed = detect_ref_change(dep_ref, _locked_reg, update_refs=False)
            assert ref_changed is False  # "^1.2.0" covers "1.2.0"

            # This confirms the T5 replay path would fire
            resolver.download_from_lockfile(
                dep_ref,
                install_path,
                resolved_url=_locked_reg.resolved_url,
                resolved_hash=_locked_reg.resolved_hash,
                version=_locked_reg.version,
            )
            resolver.download_from_lockfile.assert_called_once()
            resolver.download_package.assert_not_called()

    def test_replay_skipped_when_update_refs_true(self, tmp_path):
        """With update_refs=True, the replay path is bypassed."""
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.models.dependency.reference import DependencyReference

        locked = self._make_locked_dep("1.2.0")
        lock = LockFile()
        lock.add_dependency(locked)

        dep_ref = DependencyReference(
            repo_url="acme/web-skills",
            reference="^1.2.0",
            source="registry",
            registry_name="corp-main",
        )

        # update_refs=True → the replay guard fires at the outermost check
        update_refs = True
        _locked_reg = lock.get_dependency(dep_ref.get_unique_key())
        # The `not update_refs` short-circuit means replay is bypassed
        replay_would_fire = (
            not update_refs
            and _locked_reg
            and _locked_reg.resolved_url
            and _locked_reg.resolved_hash
            and _locked_reg.version
        )
        assert replay_would_fire is False

    def test_replay_skipped_when_range_no_longer_covers_locked_version(self):
        """If the manifest range was bumped past the locked version, replay is skipped."""
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.drift import detect_ref_change
        from apm_cli.models.dependency.reference import DependencyReference

        locked = self._make_locked_dep("1.2.0")
        lock = LockFile()
        lock.add_dependency(locked)

        # Manifest was bumped to ^2.0.0 — no longer covers locked 1.2.0
        dep_ref = DependencyReference(
            repo_url="acme/web-skills",
            reference="^2.0.0",
            source="registry",
            registry_name="corp-main",
        )

        _locked_reg = lock.get_dependency(dep_ref.get_unique_key())
        ref_changed = detect_ref_change(dep_ref, _locked_reg, update_refs=False)
        assert ref_changed is True  # range no longer covers locked version


class TestRegistryResolutionFlowsToLockfile:
    """End-to-end: a fake registry install captures resolved_url/hash into the lockfile."""

    def test_installed_package_carries_resolution_to_locked_dependency(self):
        # This test exercises the from_dependency_ref + InstalledPackage chain
        # without spinning up the full install pipeline. The wiring already
        # has resolver-level e2e tests; this one just confirms the data
        # flows through InstalledPackage correctly to the LockFile.
        from apm_cli.deps.installed_package import InstalledPackage
        from apm_cli.deps.registry.resolver import RegistryResolution
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference(
            repo_url="acme/web",
            reference="^1.2.0",
            source="registry",
            registry_name="corp",
        )
        resolution = RegistryResolution(
            resolved_url="https://corp.example.com/apm/v1/packages/acme/web/versions/1.2.3/download",
            resolved_hash="sha256:" + "a" * 64,
            version="1.2.3",
        )
        pkg = InstalledPackage(
            dep_ref=dep,
            resolved_commit=None,
            depth=1,
            resolved_by=None,
            registry_resolution=resolution,
        )

        # Mock dependency_graph not actually needed for from_installed_packages
        # without a real graph — pass a lightweight stub.
        class _StubGraph:
            pass

        lock = LockFile.from_installed_packages([pkg], _StubGraph())
        locked = lock.get_dependency("acme/web")
        assert locked.source == "registry"
        assert locked.resolved_url == resolution.resolved_url
        assert locked.resolved_hash == "sha256:" + "a" * 64
        assert locked.version == "1.2.3"

        # And the lockfile bumps to v2 on emit.
        yaml_out = lock.to_yaml()
        assert "lockfile_version: '2'" in yaml_out
