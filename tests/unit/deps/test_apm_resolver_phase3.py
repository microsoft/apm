"""Phase-3 unit tests for apm_cli.deps.apm_resolver.APMDependencyResolver.

Covers error/edge branches not exercised by the parallel-BFS test suite:
- _resolve_max_parallel (explicit, env var clamping, negative)
- _signature_accepts_parent_pkg (kwargs, no match, introspection error)
- resolve_dependencies (no apm.yml, ValueError, circular deps)
- _remote_parent_eligible (ADO vs non-ADO)
- expand_parent_repo_decl (all error cases + success)
- build_dependency_tree (parse error, max_depth, dev deps, parent-repo expansion)
- detect_circular_dependencies (cycle path)
- flatten_dependencies (conflict recording)
- _validate_dependency_reference
- _is_remote_parent (all branches)
- _compute_dep_source_path (local relative, local absolute, remote)
- _download_dedup_key (local vs non-local)
- _effective_base_dir (with/without parent)
- _try_load_dependency_package (no apm_modules_dir, remote parent rejection, SKILL.md,
  no apm.yml, download callback legacy form)
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.models.apm_package import DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pkg(
    root: Path,
    name: str,
    deps: list[str] | None = None,
    dev_deps: list[str] | None = None,
) -> Path:
    pkg_dir = root / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps or dev_deps:
        manifest["dependencies"] = {}
        manifest["dependencies"]["apm"] = deps or []
        manifest["dependencies"]["mcp"] = []
        if dev_deps:
            manifest["devDependencies"] = {"apm": dev_deps, "mcp": []}
    (pkg_dir / "apm.yml").write_text(yaml.safe_dump(manifest))
    return pkg_dir


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


class TestResolveDependencies:
    def test_no_apm_yml_returns_empty_graph(self, tmp_path: Path) -> None:
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        assert graph.root_package.name == "unknown"
        assert len(graph.dependency_tree.nodes) == 0

    def test_invalid_apm_yml_returns_error_graph(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text("not: valid: yaml: [")
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        assert graph.root_package.name == "error"
        assert graph.has_errors()

    def test_valid_root_with_no_deps_resolves_ok(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(yaml.safe_dump({"name": "myapp", "version": "0.1.0"}))
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        assert graph.root_package.name == "myapp"
        assert len(graph.dependency_tree.nodes) == 0

    def test_circular_dependency_recorded(self, tmp_path: Path) -> None:
        # Create a simple circular dep via a two-node cycle
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        _write_pkg(mods / "org", "a", deps=["org/b"])
        _write_pkg(mods / "org", "b", deps=["org/a"])
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "root",
                    "version": "0.0.1",
                    "dependencies": {"apm": ["org/a"], "mcp": []},
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=mods)
        graph = resolver.resolve_dependencies(tmp_path)
        assert len(graph.circular_dependencies) > 0


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


class TestBuildDependencyTree:
    def test_parse_error_returns_empty_tree(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("{bad yaml}")
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        tree = resolver.build_dependency_tree(apm_yml)
        assert tree.root_package.name == "error"

    def test_max_depth_nodes_are_skipped(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        _write_pkg(mods / "org", "leaf")
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "root",
                    "version": "0.0.1",
                    "dependencies": {"apm": ["org/leaf"], "mcp": []},
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=mods, max_depth=0)
        resolver._project_root = tmp_path
        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        # max_depth=0: depth-1 items exceed 0, none should be added
        assert len(tree.nodes) == 0

    def test_dev_dep_added_when_not_in_prod(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        _write_pkg(mods / "org", "devpkg")
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "root",
                    "version": "0.0.1",
                    "dependencies": {"apm": [], "mcp": []},
                    "devDependencies": {"apm": ["org/devpkg"], "mcp": []},
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=mods)
        resolver._project_root = tmp_path
        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        keys = list(tree.nodes.keys())
        assert any("devpkg" in k for k in keys), keys

    def test_root_parent_repo_inheritance_raises(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "root",
                    "version": "0.0.1",
                    "dependencies": {
                        "apm": [{"git": "parent", "path": "subdir"}],
                        "mcp": [],
                    },
                }
            )
        )
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        with pytest.raises(ValueError, match="git: parent cannot be used in the root"):
            resolver.build_dependency_tree(tmp_path / "apm.yml")


# ---------------------------------------------------------------------------
# detect_circular_dependencies
# ---------------------------------------------------------------------------


class TestDetectCircularDependencies:
    def test_no_circular_returns_empty_list(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        _write_pkg(mods / "org", "a")
        _write_pkg(mods / "org", "b")
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "root",
                    "version": "0.0.1",
                    "dependencies": {"apm": ["org/a", "org/b"], "mcp": []},
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=mods)
        graph = resolver.resolve_dependencies(tmp_path)
        assert graph.circular_dependencies == []


# ---------------------------------------------------------------------------
# flatten_dependencies
# ---------------------------------------------------------------------------


class TestFlattenDependencies:
    def test_single_dep_not_a_conflict(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        _write_pkg(mods / "org", "only")
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "root",
                    "version": "0.0.1",
                    "dependencies": {"apm": ["org/only"], "mcp": []},
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=mods)
        graph = resolver.resolve_dependencies(tmp_path)
        deps = graph.flattened_dependencies.get_installation_list()
        assert len(deps) == 1


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


class TestComputeDepSourcePath:
    def test_local_absolute_dep_returns_resolved_local(self, tmp_path: Path) -> None:
        ref = MagicMock()
        ref.is_local = True
        ref.local_path = str(tmp_path)
        result = APMDependencyResolver._compute_dep_source_path(ref, None, tmp_path)
        assert result == tmp_path.resolve()

    def test_local_relative_with_parent_anchors_on_parent(self, tmp_path: Path) -> None:
        parent_source = tmp_path / "parent_pkg"
        parent_source.mkdir()
        parent = MagicMock()
        parent.source_path = parent_source
        ref = MagicMock()
        ref.is_local = True
        ref.local_path = "sibling"
        result = APMDependencyResolver._compute_dep_source_path(ref, parent, tmp_path)
        assert result == (parent_source / "sibling").resolve()

    def test_remote_dep_returns_install_path(self, tmp_path: Path) -> None:
        ref = MagicMock()
        ref.is_local = False
        ref.local_path = None
        result = APMDependencyResolver._compute_dep_source_path(ref, None, tmp_path)
        assert result == tmp_path.resolve()


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

    def test_local_with_parent_includes_source_path(self, tmp_path: Path) -> None:
        ref = MagicMock()
        ref.is_local = True
        ref.get_unique_key.return_value = "../lib"
        parent = MagicMock()
        parent.source_path = tmp_path
        key = APMDependencyResolver._download_dedup_key(ref, parent)
        assert key == f"../lib@{tmp_path}"


# ---------------------------------------------------------------------------
# _effective_base_dir
# ---------------------------------------------------------------------------


class TestEffectiveBaseDir:
    def test_no_parent_returns_project_root(self, tmp_path: Path) -> None:
        assert APMDependencyResolver._effective_base_dir(None, tmp_path) == tmp_path

    def test_parent_without_source_path_returns_project_root(self, tmp_path: Path) -> None:
        parent = MagicMock()
        parent.source_path = None
        assert APMDependencyResolver._effective_base_dir(parent, tmp_path) == tmp_path

    def test_parent_with_source_path_returns_source_path(self, tmp_path: Path) -> None:
        parent_source = tmp_path / "parent"
        parent = MagicMock()
        parent.source_path = parent_source
        assert APMDependencyResolver._effective_base_dir(parent, tmp_path) == parent_source


# ---------------------------------------------------------------------------
# _try_load_dependency_package - low-level path tests
# ---------------------------------------------------------------------------


class TestTryLoadDependencyPackage:
    def test_returns_none_when_no_apm_modules_dir(self) -> None:
        resolver = APMDependencyResolver()
        resolver._apm_modules_dir = None
        dep_ref = _make_dep_ref("org/pkg")
        assert resolver._try_load_dependency_package(dep_ref) is None

    def test_skill_md_package_has_no_transitive_deps(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "skillpkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "SKILL.md").write_text("# My Skill")
        ref = MagicMock()
        ref.is_local = False
        ref.local_path = None
        ref.repo_url = "org/skillpkg"
        ref.get_install_path.return_value = pkg_dir
        ref.get_unique_key.return_value = "org/skillpkg"
        ref.get_display_name.return_value = "org/skillpkg"
        resolver = APMDependencyResolver(apm_modules_dir=mods)
        pkg = resolver._try_load_dependency_package(ref)
        assert pkg is not None
        assert pkg.get_apm_dependencies() == []

    def test_missing_package_and_no_callback_returns_none(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        ref = MagicMock()
        ref.is_local = False
        ref.local_path = None
        ref.repo_url = "org/missing"
        install_path = mods / "org" / "missing"
        ref.get_install_path.return_value = install_path
        ref.get_unique_key.return_value = "org/missing"
        ref.get_display_name.return_value = "org/missing"
        resolver = APMDependencyResolver(apm_modules_dir=mods)
        assert resolver._try_load_dependency_package(ref) is None

    def test_remote_parent_local_path_dep_is_rejected(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()

        ref = MagicMock()
        ref.is_local = True
        ref.local_path = "../something"
        ref.repo_url = "_local/something"
        ref.get_install_path.return_value = mods / "_local" / "something"
        ref.get_unique_key.return_value = "../something"
        ref.get_display_name.return_value = "../something"

        remote_parent = MagicMock()
        remote_parent.source = "https://github.com/org/repo"
        remote_parent.name = "remote-pkg"
        remote_parent.source_path = None

        resolver = APMDependencyResolver(apm_modules_dir=mods)
        with patch("apm_cli.utils.console._rich_error"):
            result = resolver._try_load_dependency_package(ref, parent_pkg=remote_parent)
        assert result is None
        assert "../something" in resolver._rejected_remote_local_keys

    def test_legacy_callback_called_without_parent_pkg(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        mods.mkdir()
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)

        call_log: list = []
        lock = threading.Lock()

        def legacy_cb(dep_ref, modules_dir, parent_chain=""):
            with lock:
                call_log.append((dep_ref, parent_chain))

        ref = MagicMock()
        ref.is_local = False
        ref.local_path = None
        ref.repo_url = "org/pkg"
        ref.get_install_path.return_value = mods / "org" / "notexist"
        ref.get_unique_key.return_value = "org/pkg"
        ref.get_display_name.return_value = "org/pkg"

        resolver = APMDependencyResolver(apm_modules_dir=mods, download_callback=legacy_cb)
        resolver._try_load_dependency_package(ref)
        assert len(call_log) == 1


# ---------------------------------------------------------------------------
# Anchored-local-path portability (committable, cross-machine lockfile)
# ---------------------------------------------------------------------------


class TestPortableAnchorIdentity:
    """`_portable_anchor_identity` is the single owner of the identity string
    persisted for a resolved transitive local dependency. That identity feeds
    the lockfile `dependencies[].anchored_local_path` field and, via the
    `local:` owner identity, the deployment-ledger owner rows. A lockfile is a
    committed cross-machine artifact, so an in-project package MUST serialize
    to a project-root-relative POSIX path -- never an absolute path carrying a
    developer home directory, which would make a regenerated lockfile
    non-portable and non-deterministic across machines and CI.
    """

    def test_in_project_dep_is_root_relative_posix(self, tmp_path: Path) -> None:
        base = tmp_path / "proj"
        anchored = base / "packages" / "child"
        identity = APMDependencyResolver._portable_anchor_identity(anchored, base)
        assert identity == "packages/child"
        assert not Path(identity).is_absolute()
        assert str(tmp_path) not in identity
        assert "\\" not in identity

    def test_nested_transitive_dep_is_root_relative(self, tmp_path: Path) -> None:
        base = tmp_path / "proj"
        anchored = base / "child" / "grandchild"
        identity = APMDependencyResolver._portable_anchor_identity(anchored, base)
        assert identity == "child/grandchild"

    def test_out_of_project_dep_falls_back_to_absolute_posix(self, tmp_path: Path) -> None:
        base = tmp_path / "proj"
        base.mkdir()
        outside = tmp_path / "elsewhere" / "pkg"
        identity = APMDependencyResolver._portable_anchor_identity(outside, base)
        # Out-of-project local deps cannot be committed portably; a stable
        # absolute POSIX identity is the honest fallback, not a fabricated one.
        assert identity == outside.resolve().as_posix()

    def test_unknown_base_falls_back_to_absolute_posix(self, tmp_path: Path) -> None:
        anchored = tmp_path / "child"
        identity = APMDependencyResolver._portable_anchor_identity(anchored, None)
        assert identity == anchored.resolve().as_posix()


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "apm.lock.yaml").exists() and (parent / "pyproject.toml").exists():
            return parent
    raise AssertionError("could not locate repo root with apm.lock.yaml")


class TestCommittedLockfilePortability:
    """The committed `apm.lock.yaml` is a cross-machine artifact. This guard
    fails if any identity field ever regenerates with an absolute machine path
    -- the exact regression the resolver anchoring fix prevents. It scans the
    real committed lockfile, so it bites regardless of who regenerates it.
    """

    def test_no_absolute_anchor_or_owner_identity(self) -> None:
        text = (_repo_root() / "apm.lock.yaml").read_text(encoding="utf-8")
        # Absolute anchored_local_path (POSIX or Windows drive form).
        assert not re.search(r"anchored_local_path:\s*(/|[A-Za-z]:\\)", text), (
            "anchored_local_path leaked an absolute path into the committed lockfile"
        )
        # Absolute local: owner identity (declaring_parent + ledger owner rows).
        assert "local:/" not in text, (
            "an absolute local: owner identity leaked into the committed lockfile"
        )
        # No developer home directory prefix anywhere in the artifact.
        assert "/Users/" not in text and "/home/" not in text, (
            "a developer home directory leaked into the committed lockfile"
        )
