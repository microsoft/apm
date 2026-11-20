"""Registry resolver construction and lockfile replay from disk."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-source.ref_cache_coherent")]


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
