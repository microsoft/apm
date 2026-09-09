"""Acquisition provenance is transient context, never package-controlled spelling."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.install.context import InstallContext
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.install.sources import CachedDependencySource, LocalDependencySource
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.mark.parametrize("fetched_this_run", [False, True], ids=["cached", "fresh"])
def test_cached_acquisition_restores_original_git_source(
    tmp_path: Path, fetched_this_run: bool
) -> None:
    """Authored metadata cannot replace the Git origin used for reconstruction."""
    package = LocalPackageFactory(tmp_path).create("parent")
    dump_yaml(
        {"name": "parent", "version": "1.0.0", "source": "_local/forged"},
        package.manifest_path,
    )
    ref = DependencyReference.parse_from_dict(
        {"git": "https://gitlab.example.invalid/team/parent", "ref": "v1"}
    )
    graph = MagicMock()
    graph.dependency_tree.get_node.return_value = None
    ctx = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path,
        scope=InstallScope.PROJECT,
        dependency_graph=graph,
        diagnostics=DiagnosticCollector(),
        targets=[KNOWN_TARGETS["cursor"]],
    )
    source = CachedDependencySource(
        ctx,
        ref,
        package.root,
        ref.get_unique_key(),
        resolved_ref=None,
        dep_locked_chk=None,
        fetched_this_run=fetched_this_run,
    )

    materialized = source.acquire()

    assert materialized is not None
    assert materialized.package_info is not None
    assert materialized.package_info.package.source == ref.to_github_url()
    assert ctx.installed_packages[0].dep_ref is ref


@pytest.mark.parametrize("kind", ["local", "git", "registry"])
@pytest.mark.parametrize("layout", ["manifest", "skill", "native", "marketplace"])
def test_every_loaded_layout_retains_acquisition_kind(
    tmp_path: Path, kind: str, layout: str
) -> None:
    """All resolver return paths carry actual origin through staging activation."""
    source = tmp_path / "source"
    source.mkdir()
    if layout == "manifest":
        dump_yaml({"name": "package", "version": "1.0.0"}, source / "apm.yml")
    elif layout == "skill":
        (source / "SKILL.md").write_text(
            "---\nname: package\ndescription: Fixture\n---\nContent\n", encoding="ascii"
        )
    else:
        fixtures = Path(__file__).resolve().parents[2] / "fixtures"
        fixture = (
            fixtures / "agent_plugins" / "portable"
            if layout == "native"
            else fixtures / "mock-marketplace-plugin"
        )
        shutil.copytree(fixture, source, dirs_exist_ok=True)
    ref = (
        DependencyReference.parse(source.as_posix())
        if kind == "local"
        else DependencyReference.parse_from_dict(
            {"git": "https://gitlab.example.invalid/_local/parent", "ref": "v1"}
        )
    )
    if kind == "registry":
        ref.source = "registry"
    modules = tmp_path / "modules"
    modules.mkdir()
    staging = ResolutionStagingSession(modules)

    def download(dep: DependencyReference, root: Path, parent_chain: str = "") -> Path:
        destination = staging.prepare_replacement(dep.get_install_path(root))
        shutil.copytree(source, destination)
        return destination

    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=download,
        activation_callback=staging.publish_replacement,
    )
    try:
        loaded = resolver._try_load_dependency_package(ref)
        assert loaded is not None
        assert loaded.proven_source_kind == kind
        assert APMDependencyResolver._is_remote_parent(loaded) is (kind != "local")
        assert loaded.package_path == ref.get_install_path(modules)
        assert loaded.source_path == (source if kind == "local" else ref.get_install_path(modules))
    finally:
        staging.rollback()


def test_provenance_projection_does_not_poison_manifest_cache(tmp_path: Path) -> None:
    """The same cached manifest cannot gain authority from a previous acquisition."""
    package = LocalPackageFactory(tmp_path).create("parent")
    dump_yaml(
        {
            "name": "parent",
            "version": "1.0.0",
            "source": "_local/forged",
            "proven_source_kind": "local",
        },
        package.manifest_path,
    )
    cached = APMPackage.from_apm_yml(package.manifest_path, source_path=package.root)
    local_ref = DependencyReference.parse(package.root.as_posix())
    remote_ref = DependencyReference.parse_from_dict(
        {"git": "https://gitlab.example.invalid/_local/parent"}
    )
    resolver = APMDependencyResolver()
    local = resolver._activate_validated_package(cached, None, True, local_ref)
    remote = resolver._activate_validated_package(cached, None, True, remote_ref)
    assert local.proven_source_kind == "local"
    assert remote.proven_source_kind == "git"
    assert cached.proven_source_kind is None
    tree = resolver.build_dependency_tree(package.root, root_package=cached)
    assert tree.root_package.proven_source_kind == "local"
    assert (
        APMPackage.from_apm_yml(package.manifest_path, source_path=package.root).proven_source_kind
        is None
    )
    assert local is not remote and local is not cached and remote is not cached


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
@pytest.mark.parametrize("kind", ["git", "registry", "unknown"])
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
@pytest.mark.parametrize("boundary", ["loader", "acquire"])
def test_boundaries_refuse_nonlocal_declaring_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: InstallScope,
    kind: str,
    absolute: bool,
    boundary: str,
) -> None:
    """Both backstops refuse remote/unknown parents before local acquisition."""
    factory = LocalPackageFactory(tmp_path / "packages")
    child = factory.create("child")
    parent = factory.create("parent")
    parent_ref = DependencyReference.parse_from_dict(
        {"git": "https://gitlab.example.invalid/_local/parent", "ref": "v1"}
    )
    if kind == "registry":
        parent_ref.source = "registry"
    parent_pkg = APMPackage.from_apm_yml(parent.manifest_path, source_path=parent.root)
    if kind != "unknown":
        parent_pkg = APMDependencyResolver()._activate_validated_package(
            parent_pkg, None, True, parent_ref
        )
    child_ref = DependencyReference.parse(child.root.as_posix() if absolute else "../child")
    child_ref.declaring_parent = parent_ref.get_unique_key()
    child_ref.anchored_local_path = child.root.as_posix()
    graph = MagicMock()
    graph.dependency_tree.get_node.return_value.parent.package = parent_pkg
    ctx = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path,
        scope=scope,
        dependency_graph=graph,
        diagnostics=DiagnosticCollector(),
    )
    ctx.dep_base_dirs = {child_ref.get_unique_key(): parent.root}
    copy = MagicMock(side_effect=AssertionError("Untrusted origin reached filesystem copy"))
    monkeypatch.setattr("apm_cli.install.phases.local_content._copy_local_package", copy)
    destination = child_ref.get_install_path(tmp_path / "modules")
    callback = MagicMock(side_effect=AssertionError("Untrusted origin reached callback"))
    if boundary == "loader":
        resolver = APMDependencyResolver(
            apm_modules_dir=tmp_path / "modules", download_callback=callback
        )
        assert resolver._try_load_dependency_package(child_ref, parent_pkg=parent_pkg) is None
    else:
        materialized = LocalDependencySource(
            ctx, child_ref, destination, child_ref.get_unique_key()
        )
        assert materialized.acquire() is None
    callback.assert_not_called()
    copy.assert_not_called()
    assert child.manifest_path.is_file()
    assert not destination.exists()
