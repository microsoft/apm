"""Unit tests for apm_cli.deps.apm_resolver.APMDependencyResolver.

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

import json
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver, DownloadedPackageError
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.apm_package import DependencyReference
from apm_cli.models.validation import PackageContentType, PackageType, validate_apm_package

pytestmark = pytest.mark.component

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


class TestManifestlessSkillBundleResolution:
    """Downloaded collections use canonical validation before staged activation (#2888)."""

    @pytest.mark.parametrize("existing", [False, True], ids=["fresh", "replacement"])
    def test_valid_bundle_activates_and_reloads(self, tmp_path: Path, existing: bool) -> None:
        """Valid nested skills need neither a root SKILL.md nor a generated manifest."""
        modules = tmp_path / "apm_modules"
        ref = DependencyReference(repo_url="org/collection", reference="main")
        live = ref.get_install_path(modules)
        if existing:
            live.mkdir(parents=True)
            (live / "old.txt").write_text("previous install", encoding="utf-8")
        staging = ResolutionStagingSession(modules)
        candidate = staging.prepare_replacement(live)
        skill = candidate / "skills/find-skills/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: find-skills\ndescription: Find skills\n---\n# Find skills\n",
            encoding="utf-8",
        )
        activate = MagicMock(side_effect=staging.publish_replacement)
        resolver = APMDependencyResolver(
            apm_modules_dir=modules,
            download_callback=lambda *_args, **_kwargs: candidate,
            activation_callback=activate,
        )

        try:
            package = resolver._try_load_dependency_package(ref)
            assert package is not None
            assert package.package_path == live.resolve()
            assert package.source == "org/collection"
            assert package.type is PackageContentType.SKILL
            assert package.version == "0.0.0"
            assert package.get_apm_dependencies() == []
            activate.assert_called_once_with(candidate)
            assert (live / "skills/find-skills/SKILL.md").is_file()
            assert not (live / "apm.yml").exists()
            assert not (live / "SKILL.md").exists()
            assert not (live / "old.txt").exists()
            reloaded = APMDependencyResolver(apm_modules_dir=modules)._try_load_dependency_package(
                ref
            )
            assert reloaded is not None
            assert reloaded.source == package.source
            assert reloaded.package_path == package.package_path
            assert staging.commit() == []
        finally:
            staging.rollback()

    @pytest.mark.parametrize("existing", [False, True], ids=["fresh", "replacement"])
    @pytest.mark.parametrize(
        "invalid", ["malformed", "invalid-manifest", "escaping-symlink", "escaping-directory"]
    )
    def test_invalid_bundle_preserves_state(
        self, tmp_path: Path, existing: bool, invalid: str
    ) -> None:
        """Invalid candidates retain validator diagnostics and never replace live bytes."""
        modules = tmp_path / "apm_modules"
        ref = DependencyReference(repo_url="org/collection", reference="main")
        live = ref.get_install_path(modules)
        if existing:
            live.mkdir(parents=True)
            (live / "old.txt").write_bytes(b"previous install")
        staging = ResolutionStagingSession(modules)
        candidate = staging.prepare_replacement(live)
        skill = candidate / "skills/find-skills/SKILL.md"
        skill.parent.mkdir(parents=True)
        sibling = candidate / "skills/valid-skill/SKILL.md"
        sibling.parent.mkdir()
        sibling.write_text(
            "---\nname: valid-skill\ndescription: Valid sibling\n---\n",
            encoding="utf-8",
        )
        outside = tmp_path / "outside/SKILL.md"
        outside.parent.mkdir()
        outside.write_bytes(b"outside must remain untouched")
        if invalid == "malformed":
            skill.write_text("---\nname: [unterminated\n---\n", encoding="utf-8")
        elif invalid == "invalid-manifest":
            skill.write_text(
                "---\nname: find-skills\ndescription: Find skills\n---\n", encoding="utf-8"
            )
            (candidate / "apm.yml").write_text("name: [unterminated\n", encoding="utf-8")
        elif invalid == "escaping-directory":
            skill.parent.rmdir()
            skill.parent.symlink_to(outside.parent, target_is_directory=True)
        else:
            skill.symlink_to(outside)
        validation = validate_apm_package(candidate)
        assert validation.package_type is PackageType.SKILL_BUNDLE
        assert validation.is_valid is False
        if invalid != "invalid-manifest":
            assert validation.package is not None
        activate = MagicMock(side_effect=staging.publish_replacement)
        resolver = APMDependencyResolver(
            apm_modules_dir=modules,
            download_callback=lambda *_args, **_kwargs: candidate,
            activation_callback=activate,
        )

        try:
            with pytest.raises(DownloadedPackageError) as error:
                resolver._try_load_dependency_package(ref)
            # #3871: staged paths are shown relative to the package, one period.
            expected = APMDependencyResolver._package_relative_detail(
                "; ".join(validation.errors), candidate
            ).rstrip(".")
            assert expected in str(error.value)
            assert str(candidate) not in str(error.value)
            assert ".. " not in str(error.value)
            hint = (
                "existing installation remains active"
                if existing
                else "downloaded candidate was not activated"
            )
            assert hint in str(error.value)
            activate.assert_not_called()
        finally:
            assert staging.rollback() == []
        assert not candidate.exists()
        assert outside.read_bytes() == b"outside must remain untouched"
        if existing:
            assert sorted(path.name for path in live.iterdir()) == ["old.txt"]
            assert (live / "old.txt").read_bytes() == b"previous install"
        else:
            assert not live.exists()


# ---------------------------------------------------------------------------
# _resolve_max_parallel
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _signature_accepts_parent_pkg
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# expand_parent_repo_decl
# ---------------------------------------------------------------------------


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

    @pytest.mark.req("req-mf-010")
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


# ---------------------------------------------------------------------------
# _is_remote_parent
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _compute_dep_source_path
# ---------------------------------------------------------------------------


class TestComputeDepSourcePath:
    def test_local_absolute_dep_returns_resolved_local(self, tmp_path: Path) -> None:
        ref = DependencyReference.parse(str(tmp_path))
        result = APMDependencyResolver._compute_dep_source_path(ref, None, tmp_path)
        assert result == tmp_path.resolve()

    def test_local_relative_with_parent_anchors_on_parent(self, tmp_path: Path) -> None:
        parent_source = tmp_path / "parent_pkg"
        parent_source.mkdir()
        parent = MagicMock()
        parent.source_path = parent_source
        ref = DependencyReference(
            repo_url="_local/sibling", source="local", is_local=True, local_path="sibling"
        )
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
    def test_local_with_parent_includes_source_path(self, tmp_path: Path) -> None:
        ref = DependencyReference.parse("../lib")
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

    def test_native_agent_plugin_without_apm_yml_returns_projected_package(
        self, tmp_path: Path
    ) -> None:
        from apm_cli.agent_plugins import PLUGIN_SCHEMA_ID

        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "native"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": PLUGIN_SCHEMA_ID,
                    "name": "native.plugin",
                    "version": "2.0.0",
                }
            ),
            encoding="utf-8",
        )
        ref = _make_dep_ref("org/native")
        ref.get_install_path.return_value = pkg_dir

        pkg = APMDependencyResolver(apm_modules_dir=mods)._try_load_dependency_package(ref)

        assert pkg is not None
        assert pkg.name == "native.plugin"
        assert pkg.version == "2.0.0"
        assert pkg.agent_plugin is not None
        assert not (pkg_dir / "apm.yml").exists()

    def test_eligible_apm_manifest_wins_and_retains_transitive_dependencies(
        self, tmp_path: Path
    ) -> None:
        from apm_cli.agent_plugins import PLUGIN_SCHEMA_ID

        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "native"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": PLUGIN_SCHEMA_ID,
                    "name": "native.plugin",
                    "version": "2.0.0",
                }
            ),
            encoding="utf-8",
        )
        (pkg_dir / "apm.yml").write_text(
            yaml.safe_dump(
                {
                    "name": "apm-native",
                    "version": "3.0.0",
                    "dependencies": {"apm": ["org/child#v1.0.0"]},
                }
            ),
            encoding="utf-8",
        )
        ref = _make_dep_ref("org/native")
        ref.get_install_path.return_value = pkg_dir

        pkg = APMDependencyResolver(apm_modules_dir=mods)._try_load_dependency_package(ref)

        assert pkg is not None
        assert pkg.name == "apm-native"
        assert pkg.version == "3.0.0"
        assert pkg.agent_plugin is None
        assert [dep.repo_url for dep in pkg.get_apm_dependencies()] == ["org/child"]

    def test_malformed_root_plugin_fails_closed_without_apm_yml(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "malformed"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "plugin.json").write_text("{bad json", encoding="utf-8")
        ref = _make_dep_ref("org/malformed")
        ref.get_install_path.return_value = pkg_dir

        with pytest.raises(ValueError, match=r"Invalid root plugin.json"):
            APMDependencyResolver(apm_modules_dir=mods)._try_load_dependency_package(ref)

        assert not (pkg_dir / "apm.yml").exists()

    def test_nonregular_root_plugin_fails_closed_without_apm_yml(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "nonregular"
        (pkg_dir / "plugin.json").mkdir(parents=True)
        ref = _make_dep_ref("org/nonregular")
        ref.get_install_path.return_value = pkg_dir

        with pytest.raises(ValueError, match=r"plugin.json must be a regular file"):
            APMDependencyResolver(apm_modules_dir=mods)._try_load_dependency_package(ref)

        assert not (pkg_dir / "apm.yml").exists()

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

        ref = DependencyReference.parse("../something")

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


class TestTryLoadDependencyPackageForceRecheck:
    """End-to-end through _try_load_dependency_package: an existing install
    path must still invoke download_callback when _should_force_recheck is
    True -- this is what makes a transitive dependency's own semver range
    get re-evaluated during apm update, not just direct dependencies."""

    def _semver_dep_ref(self, install_path: Path, key: str = "org/pkg"):
        ref = MagicMock()
        ref.is_local = False
        ref.local_path = None
        ref.artifactory_prefix = None
        ref.ref_kind = "semver"
        ref.repo_url = key
        ref.get_install_path.return_value = install_path
        ref.get_unique_key.return_value = key
        ref.get_display_name.return_value = key
        return ref

    def test_existing_path_calls_callback_when_update_refs_true(self, tmp_path: Path) -> None:
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")

        call_log: list = []

        def cb(dep_ref, modules_dir, parent_chain="", parent_pkg=None):
            call_log.append(dep_ref)
            return pkg_dir  # unchanged -- still exists, same content

        ref = self._semver_dep_ref(pkg_dir)
        resolver = APMDependencyResolver(
            apm_modules_dir=mods, download_callback=cb, update_refs=True
        )
        result = resolver._try_load_dependency_package(ref)

        assert len(call_log) == 1
        assert result is not None

    def test_failed_replacement_does_not_reuse_existing_path(self, tmp_path: Path) -> None:
        """A failed refresh must not silently validate the old materialization."""
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        ref = self._semver_dep_ref(pkg_dir)
        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=lambda *_args, **_kwargs: None,
            update_refs=True,
        )

        assert resolver._try_load_dependency_package(ref) is None
        assert (pkg_dir / "apm.yml").exists()

    def test_callback_exception_does_not_reuse_existing_path(self, tmp_path: Path) -> None:
        """A callback exception must fail closed instead of validating stale bytes."""
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        ref = self._semver_dep_ref(pkg_dir)

        def fail_callback(*_args, **_kwargs):
            raise RuntimeError("injected replacement failure")

        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=fail_callback,
            update_refs=True,
        )

        assert resolver._try_load_dependency_package(ref) is None
        assert (pkg_dir / "apm.yml").exists()

    def test_invalid_candidate_is_not_activated(self, tmp_path: Path) -> None:
        """A downloaded candidate must pass validation before publication."""
        mods = tmp_path / "apm_modules"
        live = mods / "org" / "pkg"
        live.mkdir(parents=True)
        (live / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        candidate = mods / ".staging" / "org" / "pkg"
        candidate.mkdir(parents=True)
        (candidate / "apm.yml").write_text("not: a-package\n")
        activated: list[Path] = []
        ref = self._semver_dep_ref(live)
        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=lambda *_args, **_kwargs: candidate,
            activation_callback=lambda path: activated.append(path) or live,
            update_refs=True,
        )

        with pytest.raises(DownloadedPackageError, match="existing installation remains active"):
            resolver._try_load_dependency_package(ref)

        assert activated == []
        assert (live / "apm.yml").read_text() == "name: pkg\nversion: 1.0.0\n"

    def test_invalid_fresh_candidate_does_not_claim_existing_install(
        self,
        tmp_path: Path,
    ) -> None:
        """Fresh-install validation guidance must not claim prior content exists."""
        mods = tmp_path / "apm_modules"
        live = mods / "org" / "pkg"
        candidate = mods / ".staging" / "org" / "pkg"
        candidate.mkdir(parents=True)
        (candidate / "apm.yml").write_text("not: a-package\n")
        ref = self._semver_dep_ref(live)
        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=lambda *_args, **_kwargs: candidate,
            activation_callback=lambda _path: live,
            update_refs=True,
        )

        with pytest.raises(DownloadedPackageError) as exc_info:
            resolver._try_load_dependency_package(ref)

        message = str(exc_info.value)
        assert "downloaded candidate was not activated" in message
        assert "existing installation remains active" not in message

    def test_valid_candidate_is_activated_after_validation(self, tmp_path: Path) -> None:
        """A valid candidate publishes once and exposes the live package path."""
        mods = tmp_path / "apm_modules"
        live = mods / "org" / "pkg"
        candidate = mods / ".staging" / "org" / "pkg"
        candidate.mkdir(parents=True)
        (candidate / "apm.yml").write_text("name: pkg\nversion: 2.0.0\n")
        activated: list[Path] = []
        ref = self._semver_dep_ref(live)

        def activate(path: Path) -> Path:
            activated.append(path)
            live.parent.mkdir(parents=True)
            path.replace(live)
            return live

        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=lambda *_args, **_kwargs: candidate,
            activation_callback=activate,
            update_refs=True,
        )

        package = resolver._try_load_dependency_package(ref)

        assert activated == [candidate]
        assert package is not None
        assert package.version == "2.0.0"
        assert package.package_path == live

    def test_legacy_marketplace_candidate_is_normalized_before_activation(
        self,
        tmp_path: Path,
    ) -> None:
        """A staged legacy plugin is normalized with its source anchor before publication."""
        mods = tmp_path / "apm_modules"
        live = mods / "org" / "legacy"
        candidate = mods / ".staging" / "org" / "legacy"
        candidate.mkdir(parents=True)
        (candidate / "plugin.json").write_text(
            json.dumps({"name": "legacy", "version": "2.0.0"}),
            encoding="utf-8",
        )
        activated: list[Path] = []
        ref = self._semver_dep_ref(live, key="org/legacy")

        def activate(path: Path) -> Path:
            activated.append(path)
            live.parent.mkdir(parents=True)
            path.replace(live)
            return live

        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=lambda *_args, **_kwargs: candidate,
            activation_callback=activate,
            update_refs=True,
        )

        package = resolver._try_load_dependency_package(ref)

        assert activated == [candidate]
        assert package is not None
        assert package.package_path == live
        assert package.source_path == live.resolve()
        assert (live / "apm.yml").is_file()
        reloaded = APMDependencyResolver(apm_modules_dir=mods)._try_load_dependency_package(ref)
        assert reloaded is not None
        assert reloaded.package_path == live
        assert reloaded.source_path == live.resolve()

    def test_cached_local_package_is_not_activated_as_candidate(self, tmp_path: Path) -> None:
        """A live local cache hit remains readable for transitive resolution."""
        mods = tmp_path / "apm_modules"
        live = mods / "_local" / "parent"
        live.mkdir(parents=True)
        (live / "apm.yml").write_text(
            "name: parent\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../child\n",
        )
        ref = DependencyReference(
            repo_url="_local/parent",
            is_local=True,
            local_path="../parent",
        )
        activated: list[Path] = []
        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=lambda *_args, **_kwargs: live,
            activation_callback=lambda path: activated.append(path) or live,
        )

        package = resolver._try_load_dependency_package(ref)

        assert package is not None
        assert len(package.get_apm_dependencies()) == 1
        assert activated == []

    def test_existing_path_without_lock_calls_callback_on_plain_install(
        self,
        tmp_path: Path,
    ) -> None:
        """Plain install must not trust an existing path without provenance."""
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")

        call_log: list = []

        def cb(dep_ref, modules_dir, parent_chain="", parent_pkg=None):
            call_log.append(dep_ref)
            return pkg_dir

        ref = self._semver_dep_ref(pkg_dir)
        resolver = APMDependencyResolver(
            apm_modules_dir=mods, download_callback=cb, update_refs=False
        )
        result = resolver._try_load_dependency_package(ref)

        assert len(call_log) == 1
        assert result is not None

    def test_existing_path_calls_callback_for_changed_literal_ref(
        self,
        tmp_path: Path,
    ) -> None:
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="org/pkg",
                resolved_ref="a" * 40,
                resolved_commit="a" * 40,
            )
        )
        calls: list[DependencyReference] = []

        def cb(dep_ref, modules_dir, parent_chain="", parent_pkg=None):
            calls.append(dep_ref)
            return pkg_dir

        ref = DependencyReference(repo_url="org/pkg", reference="b" * 40)
        resolver = APMDependencyResolver(
            apm_modules_dir=mods,
            download_callback=cb,
            existing_lockfile=lockfile,
        )

        assert resolver._try_load_dependency_package(ref) is not None
        assert calls == [ref]

    def test_existing_path_without_lock_calls_callback_for_literal_ref(
        self, tmp_path: Path
    ) -> None:
        """Missing provenance wins over literal-ref update optimization."""
        mods = tmp_path / "apm_modules"
        pkg_dir = mods / "org" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")

        call_log: list = []

        def cb(dep_ref, modules_dir, parent_chain="", parent_pkg=None):
            call_log.append(dep_ref)
            return pkg_dir

        ref = self._semver_dep_ref(pkg_dir)
        ref.ref_kind = "literal"
        resolver = APMDependencyResolver(
            apm_modules_dir=mods, download_callback=cb, update_refs=True
        )
        result = resolver._try_load_dependency_package(ref)

        assert len(call_log) == 1
        assert result is not None


def test_downloaded_package_error_names_package_paths_and_ends_once(tmp_path):
    """#3871: no staging path and no doubled period in dependency errors."""
    candidate = tmp_path / "apm_modules" / ".apm-resolution-staging" / "abc" / "replacements" / "x"
    candidate.mkdir(parents=True)
    detail = (
        f"{candidate / 'apm.yml'} uses the YAML anchor '&v' on line 2. Write it out, then retry."
    )
    with pytest.raises(DownloadedPackageError) as error:
        APMDependencyResolver._raise_downloaded_package_error(
            candidate, DependencyReference.parse("owner/repo"), detail, False
        )
    assert str(error.value) == (
        "Downloaded dependency 'owner/repo' is invalid: apm.yml uses the YAML anchor '&v' "
        "on line 2. Write it out. The downloaded candidate was not activated; "
        "fix the cause and retry the install."
    )


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
