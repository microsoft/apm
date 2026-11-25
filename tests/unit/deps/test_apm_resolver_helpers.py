"""APMDependencyResolver helper decisions that need no package tree."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.local_identity import (
    portable_anchor_identity,
    root_local_dirs,
    transitive_local_identity,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.path_anchoring import LocalResolutionError, resolve_local_dep_dir
from apm_cli.models.apm_package import DependencyReference

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-git-ref-freshness", "P-OWN-host-reference-coordinates"),
]

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


class TestOneAnchoredDirectoryIsOneDependency:
    """#4731: a library reached as ``./packages/lib`` and ``../lib`` is one package."""

    @pytest.mark.parametrize(
        ("declared", "expected_key"),
        [("../lib", "./packages/lib"), ("../other", "local:packages/other")],
    )
    def test_root_declared_directory_wins_its_spelling(
        self, tmp_path: Path, declared: str, expected_key: str
    ) -> None:
        root_ref = DependencyReference.parse("./packages/lib")
        dirs = root_local_dirs([root_ref], tmp_path)

        edge = transitive_local_identity(
            DependencyReference.parse(declared),
            declaring_parent="_local/sib",
            anchored=(tmp_path / "packages" / "sib" / declared).resolve(),
            root=tmp_path,
            root_local_dirs=dirs,
        )

        assert edge.get_unique_key() == expected_key

    @staticmethod
    def _edge(tmp_path: Path, declared: str, roots: dict) -> DependencyReference:
        return transitive_local_identity(
            DependencyReference.parse(f"../{declared}"),
            declaring_parent="_local/d",
            anchored=(tmp_path / "vendor" / "d" / ".." / declared).resolve(),
            root=tmp_path,
            root_local_dirs=roots,
        )

    @pytest.mark.parametrize(
        ("stored", "other"),
        [("F-Pkg", "f-pkg"), ("f\u00e9", "fe\u0301")],
        ids=["case", "nfd"],
    )
    @pytest.mark.parametrize(
        "volume", ["folding", "sensitive", "exact-entry"], ids=lambda volume: volume
    )
    def test_spellings_of_one_directory_are_one_node_only_where_the_volume_folds(
        self, tmp_path: Path, monkeypatch, stored: str, other: str, volume: str
    ) -> None:
        """#5424: the fold probe (samefile) decides, an exact entry always wins.

        ``folding``: *other* opens *stored*, so it takes its identity (and the
        root's). ``sensitive``: the probe denies it, so it never merges.
        ``exact-entry``: *other* is its own entry, kept even when the probe agrees.
        """
        import apm_cli.utils.paths as paths_module

        if sys.platform == "win32" and volume != "folding" and stored.lower() == other.lower():
            pytest.skip("Windows resolves and compares paths caselessly: no case-only spelling")
        vendor = tmp_path / "vendor"
        (vendor / stored).mkdir(parents=True)
        # The fold probe the volume answers: samefile on two spellings.
        monkeypatch.setattr(os.path, "samefile", lambda *_: volume != "sensitive")
        if volume == "exact-entry":
            listdir = paths_module.os.listdir
            monkeypatch.setattr(
                paths_module.os,
                "listdir",
                lambda path: [*listdir(path), other] if Path(path) == vendor else listdir(path),
            )
        root_ref = DependencyReference.parse(f"./vendor/{stored}")
        dirs = root_local_dirs([root_ref], tmp_path)

        assert self._edge(tmp_path, stored, {}).get_unique_key() == f"local:vendor/{stored}"
        if volume == "folding":
            assert self._edge(tmp_path, other, {}).get_unique_key() == f"local:vendor/{stored}"
            assert self._edge(tmp_path, other, dirs).get_unique_key() == "./vendor/" + stored
        else:
            assert self._edge(tmp_path, other, {}).get_unique_key() == f"local:vendor/{other}"
            assert self._edge(tmp_path, other, dirs).get_unique_key() == f"local:vendor/{other}"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
    def test_a_link_out_of_the_project_is_never_listed(self, tmp_path: Path, monkeypatch) -> None:
        """#5424: the stored-spelling lookup lists only directories below the project root."""
        import apm_cli.utils.paths as paths_module

        project, outside = tmp_path / "project", tmp_path / "outside"
        (outside / "Ext").mkdir(parents=True)
        (project / "vendor" / "d").mkdir(parents=True)
        try:
            (project / "vendor" / "ext").symlink_to(outside / "Ext", target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are not permitted here")
        listed: list[Path] = []
        listdir = paths_module.os.listdir
        monkeypatch.setattr(
            paths_module.os, "listdir", lambda path: listed.append(Path(path)) or listdir(path)
        )

        edge = self._edge(project, "ext", {})

        # No repository, and tmp_path is below / and HOME's ancestors: ../ (#5418).
        assert edge.anchored_local_path == "../outside/Ext"
        assert not listed, listed

    @pytest.mark.parametrize(
        ("legacy", "volume_folds", "matched"),
        [
            ("vendor/f-pkg", True, True),
            ("vendor/f-pkg", False, False),
            ("../vendor/f-pkg", True, False),
        ],
        ids=["folding", "case-sensitive", "outside-root"],
    )
    def test_a_pre_fix_row_spelled_another_way_is_read_only_where_the_volume_folds(
        self, tmp_path: Path, monkeypatch, legacy: str, volume_folds: bool, matched: bool
    ) -> None:
        """#5424: --frozen finds a pre-fix row only when samefile proves one directory."""
        from apm_cli.deps import local_identity

        (tmp_path / "vendor" / "F-Pkg").mkdir(parents=True)
        monkeypatch.setattr(local_identity.os.path, "samefile", lambda *_: volume_folds)
        row = LockedDependency(
            repo_url="_local/f-pkg",
            source="local",
            local_path="../f-pkg",
            declaring_parent="./vendor/d",
            anchored_local_path=legacy,
        )
        edge = self._edge(tmp_path, "F-Pkg", {})

        found = local_identity.locked_spelling(edge, {f"local:{legacy}": row}, tmp_path)

        assert edge.anchored_local_path == "vendor/F-Pkg"
        assert found.anchored_local_path == (legacy if matched else "vendor/F-Pkg")
        assert (
            local_identity.locked_spelling(found, {found.get_unique_key(): row}, tmp_path) is found
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="a backslash separates on Windows")
    def test_a_posix_name_holding_a_backslash_keeps_its_identity(self, tmp_path: Path) -> None:
        """#5424: ``a\\b`` is one POSIX name, never the path ``a/b``."""
        (tmp_path / "vendor" / "a\\b").mkdir(parents=True)
        (tmp_path / "vendor" / "a" / "b").mkdir(parents=True)

        edge = self._edge(tmp_path, "a\\b", {})

        assert edge.get_unique_key() == "local:vendor/a\\b"

    @staticmethod
    def _lockfile(*rows: tuple[str, str, str | None]) -> LockFile:
        lockfile = LockFile()
        for repo_url, local_path, resolved_by in rows:
            lockfile.add_dependency(
                LockedDependency(
                    repo_url=repo_url,
                    source="local",
                    local_path=local_path,
                    resolved_by=resolved_by,
                )
            )
        return lockfile

    def test_lockfile_parents_anchoring_to_one_directory_are_one_parent(
        self, tmp_path: Path
    ) -> None:
        lockfile = self._lockfile(
            ("_local/sib", "./packages/sib", None),
            ("_local/lib", "./packages/lib", None),
            ("_local/lib", "../lib", "_local/sib"),
            ("_local/leaf", "../leaf", "_local/lib"),
        )
        leaf = lockfile.get_dependency("../leaf")

        resolved = resolve_local_dep_dir(leaf, lockfile, tmp_path)

        assert resolved == (tmp_path / "packages" / "leaf").resolve()

    def test_lockfile_parents_in_different_directories_still_refuse(self, tmp_path: Path) -> None:
        lockfile = self._lockfile(
            ("_local/lib", "./packages/lib", None),
            ("_local/lib", "./vendor/lib", None),
            ("_local/leaf", "../leaf", "_local/lib"),
        )

        with pytest.raises(LocalResolutionError, match="different directories"):
            resolve_local_dep_dir(lockfile.get_dependency("../leaf"), lockfile, tmp_path)


def _monorepo(root: Path, *, dev_lib: bool) -> Path:
    """root -> ./packages/sib (-> ../lib -> ../leaf), and ./packages/lib directly."""
    for name, deps in (("sib", ["../lib"]), ("lib", ["../leaf"]), ("leaf", [])):
        package = root / "packages" / name
        package.mkdir(parents=True)
        listed = "".join(f"    - {dep}\n" for dep in deps)
        apm = f"dependencies:\n  apm:\n{listed}" if deps else ""
        (package / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0\n{apm}", encoding="utf-8")
    lib_section = "devDependencies" if dev_lib else "dependencies"
    sib = "dependencies:\n  apm:\n    - ./packages/sib\n"
    lib = f"{lib_section}:\n  apm:\n    - ./packages/lib\n"
    body = (
        sib + lib
        if dev_lib
        else "dependencies:\n  apm:\n    - ./packages/sib\n    - ./packages/lib\n"
    )
    (root / "apm.yml").write_text(f"name: mono\nversion: 1.0.0\n{body}", encoding="utf-8")
    return root / "apm.yml"


def _copy_local(calls: list[Path]):
    """Download callback that materializes a local package like install does."""
    import shutil

    def download(dep_ref, modules_dir, parent_chain, parent_pkg=None):
        base = APMDependencyResolver._effective_base_dir(parent_pkg, modules_dir.parent)
        source = (base / dep_ref.local_path).resolve()
        calls.append(source)
        destination = dep_ref.get_install_path(modules_dir)
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return destination

    return download


class TestOneAnchoredDirectoryNode:
    """#4731: the collapsed node keeps install semantics for dev status and rechecks."""

    def test_production_sibling_promotes_a_dev_library_and_its_descendants(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        manifest = _monorepo(tmp_path, dev_lib=True)
        calls: list[Path] = []
        resolver = APMDependencyResolver(
            apm_modules_dir=tmp_path / "apm_modules", download_callback=_copy_local(calls)
        )
        resolver._project_root = tmp_path

        tree = resolver.build_dependency_tree(manifest)

        assert not tree.resolution_errors, tree.resolution_errors
        dev = {node.dependency_ref.get_unique_key(): node.is_dev for node in tree.nodes.values()}
        assert dev == {
            "./packages/sib": False,
            "./packages/lib": False,
            "local:packages/leaf": False,
        }

    def test_targeted_sibling_update_rechecks_the_library_at_its_own_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        manifest = _monorepo(tmp_path, dev_lib=False)
        calls: list[Path] = []
        resolver = APMDependencyResolver(
            apm_modules_dir=tmp_path / "apm_modules",
            download_callback=_copy_local(calls),
            update_keys={"./packages/sib"},
        )
        resolver._project_root = tmp_path

        tree = resolver.build_dependency_tree(manifest)

        assert not tree.resolution_errors, tree.resolution_errors
        assert (tmp_path / "packages" / "sib" / "packages" / "lib") not in calls, calls
        assert (tmp_path / "packages" / "lib").resolve() in calls, calls


@pytest.mark.parametrize("dev_order", [("a", "lib"), ("lib", "a")], ids=["a-first", "lib-first"])
def test_production_reaches_a_shared_leaf_in_either_dev_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dev_order: tuple[str, str]
) -> None:
    """A leaf both dev roots declare is production once a production edge reaches lib."""
    monkeypatch.chdir(tmp_path)
    graph = {"sib": ["../lib"], "a": ["../leaf"], "lib": ["../leaf"], "leaf": []}
    for name, deps in graph.items():
        package = tmp_path / "packages" / name
        package.mkdir(parents=True)
        listed = "".join(f"    - {dep}\n" for dep in deps)
        apm = f"dependencies:\n  apm:\n{listed}" if deps else ""
        (package / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0\n{apm}", encoding="utf-8")
    dev = "".join(f"    - ./packages/{name}\n" for name in dev_order)
    (tmp_path / "apm.yml").write_text(
        "name: mono\nversion: 1.0.0\ndependencies:\n  apm:\n    - ./packages/sib\n"
        f"devDependencies:\n  apm:\n{dev}",
        encoding="utf-8",
    )
    resolver = APMDependencyResolver(
        apm_modules_dir=tmp_path / "apm_modules", download_callback=_copy_local([])
    )
    resolver._project_root = tmp_path

    tree = resolver.build_dependency_tree(tmp_path / "apm.yml")

    assert not tree.resolution_errors, tree.resolution_errors
    dev_flags = {node.dependency_ref.get_unique_key(): node.is_dev for node in tree.nodes.values()}
    assert dev_flags["./packages/lib"] is False, dev_flags
    assert dev_flags["local:packages/leaf"] is False, dev_flags
    assert dev_flags["./packages/a"] is True, dev_flags


class TestLegacyAbsoluteLocalIdentity:
    """#5418: a lock row keyed by a monorepo sibling's absolute path is the same
    owner as its portable ``../`` identity: read-only on the read path, re-keyed
    in place on the write path.
    """

    @staticmethod
    def _layout(root: Path) -> tuple[Path, Path, Path]:
        app = root / "R" / "apps" / "app"
        lib = root / "R" / "packages" / "lib-b"
        outside = root / "outside" / "lib-x"
        for path in (app, lib, outside):
            path.mkdir(parents=True)
        (root / "R" / ".git").mkdir()
        return app, lib, outside

    @staticmethod
    def _row(anchor: str, parent: str = "../../packages/lib-a") -> LockedDependency:
        return LockedDependency(
            repo_url=f"_local/{Path(anchor).name}",
            source="local",
            local_path=f"../{Path(anchor).name}",
            declaring_parent=parent,
            anchored_local_path=anchor,
        )

    @staticmethod
    def _ref(anchor: str) -> DependencyReference:
        from dataclasses import replace

        return replace(
            DependencyReference.parse("../lib-b"),
            declaring_parent="../../packages/lib-a",
            anchored_local_path=anchor,
        )

    def test_read_path_finds_the_absolute_row_by_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.local_identity import locked_spelling

        app, lib, outside = self._layout(tmp_path)
        legacy = lib.resolve().as_posix()
        locked = {f"local:{legacy}": self._row(legacy)}
        ref = self._ref("../../packages/lib-b")

        assert locked_spelling(ref, locked, app, directory=lib).anchored_local_path == legacy
        assert locked_spelling(ref, locked, app, directory=outside) is ref
        assert locked_spelling(ref, locked, app) is ref

    def test_read_path_never_opens_a_dotdot_identity(self, tmp_path: Path, monkeypatch) -> None:
        from apm_cli.deps import local_identity

        app, lib, _ = self._layout(tmp_path)
        legacy = lib.resolve().as_posix()
        locked = {
            f"local:{legacy}": self._row(legacy),
            "local:../../packages/other": self._row("../../packages/other"),
        }
        opened: list[str] = []
        real = os.path.samefile

        def spy(first, second):
            opened.extend((os.fspath(first), os.fspath(second)))
            return real(first, second)

        monkeypatch.setattr(local_identity.os.path, "samefile", spy)
        local_identity.locked_spelling(
            self._ref("../../packages/lib-b"), locked, app, directory=lib
        )

        assert opened
        assert not [path for path in opened if ".." in Path(path).parts], opened

    def test_write_path_renames_only_in_repository_rows_that_exist(self, tmp_path: Path) -> None:
        from apm_cli.deps.local_identity import legacy_local_renames

        app, lib, outside = self._layout(tmp_path)
        inside, out = lib.resolve().as_posix(), outside.resolve().as_posix()
        gone = (tmp_path / "R" / "packages" / "gone").as_posix()
        locked = {f"local:{path}": self._row(path) for path in (inside, out, gone)}

        assert legacy_local_renames(locked, app) == {f"local:{inside}": "../../packages/lib-b"}

    def test_rekey_moves_rows_parents_owners_and_slot(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.lockfile_rekey import rekey_legacy_local_identities

        app, lib, _ = self._layout(tmp_path)
        legacy = lib.resolve().as_posix()
        lockfile = LockFile()
        lockfile.dependencies[f"local:{legacy}"] = self._row(legacy)
        child = self._row((lib.parent / "lib-c").as_posix(), parent=f"local:{legacy}")
        lockfile.dependencies["local:outside-child"] = child
        modules = app / "apm_modules"
        slot = self._row(legacy).to_dependency_ref().get_install_path(modules)
        slot.mkdir(parents=True)
        (slot / "apm.yml").write_text("name: lib-b\n", encoding="ascii")

        journaled: list = []
        renames = rekey_legacy_local_identities(lockfile, app, modules, journal=journaled.extend)

        new = "local:../../packages/lib-b"
        assert renames == {f"local:{legacy}": new}
        assert list(lockfile.dependencies) == [new, "local:outside-child"]
        assert lockfile.dependencies[new].anchored_local_path == "../../packages/lib-b"
        assert child.declaring_parent == new
        moved = lockfile.dependencies[new].to_dependency_ref().get_install_path(modules)
        assert (moved / "apm.yml").read_text(encoding="ascii") == "name: lib-b\n"
        assert [(current, target) for current, target, _ in journaled] == [(slot, moved)]
        # The emptied <hash> parent stays until the install commits, so a rollback can restore.
        assert slot.parent.is_dir() and not any(slot.parent.iterdir())
        assert rekey_legacy_local_identities(lockfile, app, modules) == {}

    def test_rekey_onto_a_key_another_row_holds_refuses_with_the_fix(self, tmp_path: Path) -> None:
        from apm_cli.install.phases.lockfile_rekey import rekey_legacy_local_identities

        app, lib, _ = self._layout(tmp_path)
        legacy = lib.resolve().as_posix()
        lockfile = LockFile()
        lockfile.dependencies[f"local:{legacy}"] = self._row(legacy)
        lockfile.dependencies["local:../../packages/lib-b"] = self._row("../../packages/lib-b")
        before = list(lockfile.dependencies)

        with pytest.raises(RuntimeError) as refused:
            rekey_legacy_local_identities(lockfile, app)

        assert "Remove the duplicate entry from apm.yml, then run 'apm install'" in str(
            refused.value
        )
        assert list(lockfile.dependencies) == before
        assert lockfile.dependencies[f"local:{legacy}"].anchored_local_path == legacy


class TestRepositoryRelativeAnchorIdentity:
    """#5418: a monorepo sibling (outside the project root, inside its
    repository) is recorded relative to the project root with ``../``, so two
    clones at different paths write one identity. Anything outside the
    repository keeps the resolved absolute identity.
    """

    @staticmethod
    def _monorepo(root: Path, git: str = "dir") -> tuple[Path, Path]:
        app = root / "apps" / "app"
        lib = root / "packages" / "lib-a"
        app.mkdir(parents=True)
        lib.mkdir(parents=True)
        if git == "dir":
            (root / ".git").mkdir()
        elif git == "file":
            (root / ".git").write_text("gitdir: ../.git/worktrees/x\n", encoding="ascii")
        return app, lib

    @pytest.fixture(autouse=True)
    def _home_elsewhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

    def test_inside_base_unchanged(self, tmp_path: Path) -> None:
        app, _ = self._monorepo(tmp_path / "repo")
        identity = portable_anchor_identity(app / "vendor" / "x", app)
        assert identity == "vendor/x"

    @pytest.mark.parametrize("git", ["dir", "file"])
    def test_sibling_in_same_repository_is_dotdot_relative(self, tmp_path: Path, git: str) -> None:
        app, lib = self._monorepo(tmp_path / "repo", git)
        identity = portable_anchor_identity(lib, app)
        assert identity == "../../packages/lib-a"

    def test_two_clones_write_one_identity(self, tmp_path: Path) -> None:
        app_a, lib_a = self._monorepo(tmp_path / "A")
        app_b, lib_b = self._monorepo(tmp_path / "nested" / "B")
        assert portable_anchor_identity(lib_a, app_a) == portable_anchor_identity(lib_b, app_b)

    def test_sibling_across_submodule_boundary_stays_absolute(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        app, lib = self._monorepo(repo)
        (repo / "packages" / ".git").write_text("gitdir: ../.git/modules/p\n", encoding="ascii")
        identity = portable_anchor_identity(lib, app)
        assert identity == lib.resolve().as_posix()
        sub_app = repo / "packages" / "app"
        sub_app.mkdir()
        identity = portable_anchor_identity(repo / "apps", sub_app)
        assert identity == (repo / "apps").resolve().as_posix()

    def test_symlinked_sibling_is_keyed_by_its_target(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        app, lib = self._monorepo(repo)
        link = repo / "linked"
        try:
            link.symlink_to(lib, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        assert portable_anchor_identity(link, app) == ("../../packages/lib-a")
        outside = tmp_path / "outside-pkg"
        outside.mkdir()
        escape = repo / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        assert portable_anchor_identity(escape, app) == (outside.resolve().as_posix())

    def test_outside_repository_stays_absolute(self, tmp_path: Path) -> None:
        app, _ = self._monorepo(tmp_path / "repo")
        outside = tmp_path / "elsewhere" / "pkg"
        outside.mkdir(parents=True)
        identity = portable_anchor_identity(outside, app)
        assert identity == outside.resolve().as_posix()

    def test_no_repository_siblings_below_shared_roots_are_relative(self, tmp_path: Path) -> None:
        """No repository: a common ancestor below /, drives and HOME keeps ``../`` (/tmp)."""
        app, lib = self._monorepo(tmp_path / "plain", git="none")
        assert portable_anchor_identity(lib, app) == "../../packages/lib-a"

    @staticmethod
    def _home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        return home

    def test_no_repository_siblings_under_a_home_subdirectory_are_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = self._home(monkeypatch, tmp_path / "h")
        app, lib = home / "work" / "app", home / "work" / "lib"
        for path in (app, lib):
            path.mkdir(parents=True)
        assert portable_anchor_identity(lib, app) == "../lib"

    def test_no_repository_siblings_directly_under_home_stay_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = self._home(monkeypatch, tmp_path / "h")
        app, lib = home / "app", home / "lib"
        for path in (app, lib):
            path.mkdir(parents=True)
        assert portable_anchor_identity(lib, app) == lib.resolve().as_posix()

    def test_no_repository_dependency_at_or_above_home_stays_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never ``../`` climbing to HOME or past it."""
        home = self._home(monkeypatch, tmp_path / "top" / "h")
        app = home / "work" / "app"
        above = tmp_path / "top" / "lib"
        for path in (app, above):
            path.mkdir(parents=True)
        assert portable_anchor_identity(home, app) == home.resolve().as_posix()
        assert portable_anchor_identity(above, app) == above.resolve().as_posix()

    def test_a_case_variant_home_spelling_is_home_where_the_volume_folds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N4: the HOME bound holds for ``~/Work`` spelled ``~/work`` on a folding volume."""
        from apm_cli.utils.executable_exclusion import is_home_or_above as home_or_above
        from apm_cli.utils.paths import spelling_key

        home = self._home(monkeypatch, tmp_path / "Users" / "Me")
        variant = tmp_path / "users" / "me"
        app, lib = variant / "app", variant / "lib"
        for path in (app, lib):
            path.mkdir(parents=True)
        real_samefile = os.path.samefile

        def folding(first, second) -> bool:
            if spelling_key(os.fspath(first)) == spelling_key(os.fspath(second)):
                return True
            return real_samefile(first, second)

        monkeypatch.setattr(os.path, "samefile", folding)
        assert home_or_above(variant, home.resolve())
        assert home_or_above(tmp_path / "users", home.resolve())
        assert portable_anchor_identity(lib, app) == lib.resolve().as_posix()
        monkeypatch.setattr(os.path, "samefile", real_samefile)
        # The real volume decides: NTFS and default APFS fold, ext4 does not.
        probe = tmp_path / "Fold-Probe"
        probe.write_text("x", encoding="ascii")
        folded = tmp_path / "fold-probe"
        volume_folds = folded.exists() and real_samefile(probe, folded)
        assert home_or_above(variant, home.resolve()) is volume_folds
        assert portable_anchor_identity(lib, app) == (
            lib.resolve().as_posix() if volume_folds else "../lib"
        )

    def test_no_repository_filesystem_root_common_ancestor_stays_absolute(
        self, tmp_path: Path
    ) -> None:
        app, _ = self._monorepo(tmp_path / "plain", git="none")
        elsewhere = Path(tmp_path.anchor) / "apm-5418-elsewhere" / "lib"
        assert portable_anchor_identity(elsewhere, app) == elsewhere.resolve().as_posix()

    @pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
    def test_no_repository_different_drive_stays_absolute(self, tmp_path: Path) -> None:
        app, _ = self._monorepo(tmp_path / "plain", git="none")
        other = Path("Z:/pkg") if tmp_path.drive.upper() != "Z:" else Path("Y:/pkg")
        identity = portable_anchor_identity(other, app)
        assert ":" in identity
        assert not identity.startswith("..")

    def test_dotfiles_home_repository_does_not_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "dotfiles-home"
        app, lib = self._monorepo(home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        identity = portable_anchor_identity(lib, app)
        assert identity == lib.resolve().as_posix()

    def test_repository_above_home_does_not_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, lib = self._monorepo(tmp_path / "top")
        home = tmp_path / "top" / "users" / "me"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        identity = portable_anchor_identity(lib, app)
        assert identity == lib.resolve().as_posix()

    @pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
    def test_different_drive_stays_absolute(self, tmp_path: Path) -> None:
        from apm_cli.deps import local_identity

        app, _ = self._monorepo(tmp_path / "repo")
        other = Path("Z:/pkg") if tmp_path.drive.upper() != "Z:" else Path("Y:/pkg")
        with patch.object(local_identity, "repository_root", return_value=tmp_path / "repo"):
            identity = local_identity.portable_anchor_identity(other, app)
        assert ":" in identity
        assert not identity.startswith("..")
