"""APMDependencyResolver helper decisions that need no package tree."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(repo_url: str = "org/pkg", *, is_local: bool = False) -> MagicMock:
    ref = MagicMock(spec=DependencyReference)
    ref.repo_url = repo_url
    ref.get_unique_key.return_value = repo_url
    ref.get_display_name.return_value = repo_url
    ref.get_identity.return_value = repo_url
    ref.is_local = is_local
    ref.local_path = None
    ref.is_parent_repo_inheritance = False
    ref.is_azure_devops.return_value = False
    return ref


# ---------------------------------------------------------------------------
# _resolve_max_parallel
# ---------------------------------------------------------------------------


class TestResolveMaxParallel:
    def test_explicit_wins(self) -> None:
        assert APMDependencyResolver._resolve_max_parallel(7) == 7

    def test_explicit_negative_clamped_to_one(self) -> None:
        assert APMDependencyResolver._resolve_max_parallel(-3) == 1

    def test_env_var_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_RESOLVE_PARALLEL", "6")
        assert APMDependencyResolver._resolve_max_parallel(None) == 6

    def test_bad_env_var_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_RESOLVE_PARALLEL", "bad")
        assert APMDependencyResolver._resolve_max_parallel(None) == 4

    def test_env_var_zero_clamped_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_RESOLVE_PARALLEL", "0")
        assert APMDependencyResolver._resolve_max_parallel(None) == 1

    def test_no_env_var_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_RESOLVE_PARALLEL", raising=False)
        assert APMDependencyResolver._resolve_max_parallel(None) == 4


# ---------------------------------------------------------------------------
# _signature_accepts_parent_pkg
# ---------------------------------------------------------------------------


class TestSignatureAcceptsParentPkg:
    def test_callback_with_parent_pkg_param_returns_true(self) -> None:
        def cb(dep_ref, mods_dir, parent_chain="", parent_pkg=None):
            pass

        assert APMDependencyResolver._signature_accepts_parent_pkg(cb) is True

    def test_callback_with_kwargs_returns_true(self) -> None:
        def cb(dep_ref, mods_dir, **kwargs):
            pass

        assert APMDependencyResolver._signature_accepts_parent_pkg(cb) is True

    def test_legacy_callback_without_parent_pkg_returns_false(self) -> None:
        def cb(dep_ref, mods_dir, parent_chain=""):
            pass

        assert APMDependencyResolver._signature_accepts_parent_pkg(cb) is False

    def test_introspection_error_returns_false(self) -> None:
        # A MagicMock cannot have its signature inspected reliably;
        # we simulate the failure explicitly.
        class Uninspectable:
            def __call__(self, *a, **kw):
                pass

        obj = Uninspectable()
        with patch("inspect.signature", side_effect=TypeError("no sig")):
            assert APMDependencyResolver._signature_accepts_parent_pkg(obj) is False


# ---------------------------------------------------------------------------
# resolve_dependencies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _remote_parent_eligible
# ---------------------------------------------------------------------------


class TestRemoteParentEligible:
    def test_non_ado_with_slash_is_eligible(self) -> None:
        ref = MagicMock()
        ref.is_azure_devops.return_value = False
        ref.repo_url = "org/repo"
        resolver = APMDependencyResolver()
        assert resolver._remote_parent_eligible(ref) is True

    def test_non_ado_without_slash_is_not_eligible(self) -> None:
        ref = MagicMock()
        ref.is_azure_devops.return_value = False
        ref.repo_url = "noslash"
        resolver = APMDependencyResolver()
        assert resolver._remote_parent_eligible(ref) is False

    def test_ado_with_enough_slashes_is_eligible(self) -> None:
        ref = MagicMock()
        ref.is_azure_devops.return_value = True
        ref.ado_repo = "MyRepo"
        ref.repo_url = "org/project/repo"
        resolver = APMDependencyResolver()
        assert resolver._remote_parent_eligible(ref) is True

    def test_ado_without_ado_repo_is_not_eligible(self) -> None:
        ref = MagicMock()
        ref.is_azure_devops.return_value = True
        ref.ado_repo = ""
        ref.repo_url = "org/project/repo"
        resolver = APMDependencyResolver()
        assert resolver._remote_parent_eligible(ref) is False


# ---------------------------------------------------------------------------
# expand_parent_repo_decl
# ---------------------------------------------------------------------------


class TestExpandParentRepoDecl:
    def _child(self, ref: str = "org/repo") -> MagicMock:
        child = MagicMock(spec=DependencyReference)
        child.is_parent_repo_inheritance = True
        child.reference = None
        child.virtual_path = "subdir"
        child.alias = None
        child.repo_url = "parent"
        child.host = None
        child.port = None
        child.explicit_scheme = None
        child.ado_organization = None
        child.ado_project = None
        child.ado_repo = None
        child.artifactory_prefix = None
        child.is_insecure = False
        child.allow_insecure = False
        child.is_virtual = False
        child.is_local = False
        child.local_path = None
        return child

    def test_raises_if_child_not_parent_inheritance(self) -> None:
        child = MagicMock()
        child.is_parent_repo_inheritance = False
        parent = MagicMock()
        resolver = APMDependencyResolver()
        with pytest.raises(ValueError, match=r"requires child_dep\.is_parent_repo_inheritance"):
            resolver.expand_parent_repo_decl(parent, child)

    def test_raises_if_parent_is_local(self) -> None:
        child = self._child()
        parent = MagicMock()
        parent.is_local = True
        parent.repo_url = "some/path"
        resolver = APMDependencyResolver()
        with pytest.raises(ValueError, match="local path"):
            resolver.expand_parent_repo_decl(parent, child)

    def test_raises_if_parent_is_local_underscore_prefix(self) -> None:
        child = self._child()
        parent = MagicMock()
        parent.is_local = False
        parent.repo_url = "_local/mypkg"
        resolver = APMDependencyResolver()
        with pytest.raises(ValueError, match="local path"):
            resolver.expand_parent_repo_decl(parent, child)

    def test_raises_if_parent_not_eligible(self) -> None:
        child = self._child()
        parent = MagicMock()
        parent.is_local = False
        parent.repo_url = "noslash"
        parent.is_azure_devops.return_value = False
        resolver = APMDependencyResolver()
        with pytest.raises(ValueError, match="remote Git parent"):
            resolver.expand_parent_repo_decl(parent, child)


# ---------------------------------------------------------------------------
# build_dependency_tree - root parse error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# detect_circular_dependencies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# flatten_dependencies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_dependency_reference
# ---------------------------------------------------------------------------


class TestValidateDependencyReference:
    def test_empty_repo_url_is_invalid(self) -> None:
        ref = MagicMock()
        ref.repo_url = ""
        resolver = APMDependencyResolver()
        assert resolver._validate_dependency_reference(ref) is False

    def test_repo_url_without_slash_is_invalid(self) -> None:
        ref = MagicMock()
        ref.repo_url = "noslash"
        resolver = APMDependencyResolver()
        assert resolver._validate_dependency_reference(ref) is False

    def test_valid_repo_url_is_valid(self) -> None:
        ref = MagicMock()
        ref.repo_url = "org/repo"
        resolver = APMDependencyResolver()
        assert resolver._validate_dependency_reference(ref) is True


# ---------------------------------------------------------------------------
# _is_remote_parent
# ---------------------------------------------------------------------------


class TestIsRemoteParent:
    def test_none_parent_returns_false(self) -> None:
        assert APMDependencyResolver._is_remote_parent(None) is False

    def test_parent_with_no_source_returns_false(self) -> None:
        pkg = MagicMock()
        pkg.source = None
        assert APMDependencyResolver._is_remote_parent(pkg) is False

    def test_local_prefix_returns_false(self) -> None:
        pkg = MagicMock()
        pkg.source = "_local/mypkg"
        assert APMDependencyResolver._is_remote_parent(pkg) is False

    def test_https_source_returns_true(self) -> None:
        pkg = MagicMock()
        pkg.source = "https://github.com/org/repo"
        assert APMDependencyResolver._is_remote_parent(pkg) is True

    def test_git_at_source_returns_true(self) -> None:
        pkg = MagicMock()
        pkg.source = "git@github.com:org/repo.git"
        assert APMDependencyResolver._is_remote_parent(pkg) is True

    def test_owner_repo_shorthand_returns_true(self) -> None:
        pkg = MagicMock()
        pkg.source = "org/repo"
        assert APMDependencyResolver._is_remote_parent(pkg) is True

    def test_relative_local_path_returns_false(self) -> None:
        pkg = MagicMock()
        pkg.source = "../relative/path"
        assert APMDependencyResolver._is_remote_parent(pkg) is False

    def test_absolute_local_path_returns_false(self) -> None:
        pkg = MagicMock()
        pkg.source = "/abs/local/path"
        assert APMDependencyResolver._is_remote_parent(pkg) is False


# ---------------------------------------------------------------------------
# _compute_dep_source_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _download_dedup_key
# ---------------------------------------------------------------------------


class TestDownloadDedupKey:
    def test_non_local_returns_unique_key(self) -> None:
        ref = MagicMock()
        ref.is_local = False
        ref.get_unique_key.return_value = "org/repo"
        assert APMDependencyResolver._download_dedup_key(ref, None) == "org/repo"

    def test_local_without_parent_returns_unique_key(self) -> None:
        ref = MagicMock()
        ref.is_local = True
        ref.get_unique_key.return_value = "../lib"
        assert APMDependencyResolver._download_dedup_key(ref, None) == "../lib"


# ---------------------------------------------------------------------------
# _effective_base_dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _try_load_dependency_package - low-level path tests
# ---------------------------------------------------------------------------


class TestTryLoadDependencyPackage:
    def test_returns_none_when_no_apm_modules_dir(self) -> None:
        resolver = APMDependencyResolver()
        resolver._apm_modules_dir = None
        dep_ref = _make_dep_ref("org/pkg")
        assert resolver._try_load_dependency_package(dep_ref) is None


class TestShouldForceRecheck:
    """``_should_force_recheck`` decides whether download_callback gets a
    chance to run for a dep whose install path already exists on disk --
    the fix for transitive semver deps never being re-evaluated against the
    remote during ``apm update`` unless something *above* them in the tree
    also happened to change. The method delegates to the same canonical drift
    owner as ``download_callback`` so literal manifest ref changes also reach
    the callback on a plain install."""

    def _dep(self, *, is_local=False, artifactory_prefix=None, ref_kind="semver"):
        return DependencyReference(
            repo_url="org/pkg",
            is_local=is_local,
            source="local" if is_local else None,
            local_path="./pkg" if is_local else None,
            reference={"semver": "^1.0.0", "literal": "stable", None: None}[ref_kind],
            artifactory_prefix=artifactory_prefix,
        )

    def test_true_without_lock_provenance_on_plain_install(self) -> None:
        resolver = APMDependencyResolver(update_refs=False)
        assert resolver._should_force_recheck(self._dep()) is True

    def test_true_for_semver_dep_when_update_refs_true(self) -> None:
        resolver = APMDependencyResolver(update_refs=True)
        assert resolver._should_force_recheck(self._dep()) is True

    def test_true_without_lock_even_for_literal_ref(self) -> None:
        """No lock provenance wins over literal-ref update optimization."""
        resolver = APMDependencyResolver(update_refs=True)
        assert resolver._should_force_recheck(self._dep(ref_kind="literal")) is True

    def test_true_without_lock_for_default_ref(self) -> None:
        resolver = APMDependencyResolver(update_refs=True)
        assert resolver._should_force_recheck(self._dep(ref_kind=None)) is True

    def test_true_for_local_dep(self) -> None:
        resolver = APMDependencyResolver(update_refs=True)
        assert resolver._should_force_recheck(self._dep(is_local=True)) is True

    def test_false_for_artifactory_proxied_dep(self) -> None:
        resolver = APMDependencyResolver(update_refs=True)
        assert resolver._should_force_recheck(self._dep(artifactory_prefix="proxy")) is False

    def test_default_constructor_is_update_refs_false(self) -> None:
        """A remote existing path without lock provenance must be rechecked."""
        resolver = APMDependencyResolver()
        assert resolver._should_force_recheck(self._dep()) is True

    def test_true_for_changed_literal_ref_on_plain_install(self) -> None:
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/pkg",
                resolved_ref="a" * 40,
                resolved_commit="a" * 40,
            )
        )
        resolver = APMDependencyResolver(existing_lockfile=lockfile)
        dep = DependencyReference(repo_url="org/pkg", reference="b" * 40)

        assert resolver._should_force_recheck(dep) is True

    def test_false_for_unchanged_literal_ref_on_plain_install(self) -> None:
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/pkg",
                resolved_ref="a" * 40,
                resolved_commit="a" * 40,
            )
        )
        resolver = APMDependencyResolver(existing_lockfile=lockfile)
        dep = DependencyReference(repo_url="org/pkg", reference="a" * 40)

        assert resolver._should_force_recheck(dep) is False


# ---------------------------------------------------------------------------
# Anchored-local-path portability (committable, cross-machine lockfile)
# ---------------------------------------------------------------------------
