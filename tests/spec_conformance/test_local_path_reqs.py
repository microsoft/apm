"""Executable req-mf-016 source anchoring, admission, and containment contracts."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.tiered_ref_resolver import RefFreshnessPolicy
from apm_cli.install.context import InstallContext
from apm_cli.install.package_resolution import user_scope_rejection_reason
from apm_cli.install.phases.local_content import _copy_local_package
from apm_cli.install.phases.resolve import _materialization, _resolve_dependencies
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.install.sources import LocalDependencySource
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.utils.path_security import PathTraversalError
from apm_cli.utils.yaml_io import dump_yaml
from tests.spec_conformance._helpers import load_yaml_fixture
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@contextmanager
def _resolved_scope(
    manifest: Path, scope: InstallScope, downloader: MagicMock | None = None
) -> Iterator[InstallContext]:
    """Run the real resolution phase with fixture transport and scoped storage."""
    root = manifest.parent
    package = APMPackage.from_apm_yml(manifest, source_path=root)
    modules = root / "apm_modules"
    modules.mkdir()
    ctx = InstallContext(
        project_root=root,
        apm_dir=root,
        apm_package=package,
        scope=scope,
        all_apm_deps=package.get_apm_dependencies(),
        apm_modules_dir=modules,
        ref_freshness_policy=RefFreshnessPolicy.REPRODUCIBLE,
        downloader=downloader if downloader is not None else MagicMock(shared_clone_cache=None),
        diagnostics=DiagnosticCollector(),
    )
    staging = ResolutionStagingSession(modules)
    try:
        _resolve_dependencies(ctx, staging, _materialization.CachedMaterializationPathReader())
        yield ctx
    finally:
        staging.rollback()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "reference", ["./child", "../child", "/child", "~/child", ".\\child", "..\\child", "~\\child"]
)
def test_local_path_prefixes_are_recognized(reference: str) -> None:
    """Recognize local spelling without confusing it with a remote coordinate."""
    dep = DependencyReference.parse(reference)
    assert dep.is_local
    assert dep.local_path == reference


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_local_sibling_uses_original_source_in_each_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: InstallScope
) -> None:
    """Both real admission consumers retain the original parent, not CWD/staging."""
    factory = LocalPackageFactory(tmp_path / "sources")
    child = factory.create("child", targets=["cursor"])
    source = factory.add_command(child, "child", "---\ndescription: child\n---\nOriginal child\n")
    parent = factory.create("parent")
    dump_yaml(load_yaml_fixture("manifest", "valid-local-parent.yml"), parent.manifest_path)
    consumer = LocalPackageFactory(tmp_path / "scope").create(
        "consumer", dependencies=[{"path": parent.root.as_posix()}]
    )
    unrelated = tmp_path / "unrelated"
    decoy = LocalPackageFactory(unrelated).create("child")
    (unrelated / "cwd").mkdir()
    monkeypatch.chdir(unrelated / "cwd")
    with _resolved_scope(consumer.manifest_path, scope) as ctx:
        modules = ctx.apm_modules_dir
        assert not ctx.callback_failures
        assert {dep.repo_url for dep in ctx.deps_to_install} == {"_local/parent", "_local/child"}
        child_ref = next(dep for dep in ctx.deps_to_install if dep.repo_url == "_local/child")
        node = ctx.dependency_graph.dependency_tree.get_node(child_ref.get_unique_key())
        assert node.parent.package.source_path == parent.root
        assert node.package.source_path == child.root
        assert not child.root.is_relative_to(consumer.root)
        assert decoy.manifest_path.is_file()
        assert child_ref.local_path == "../child"
        assert child_ref.declaring_parent == parent.root.as_posix()
        assert child_ref.anchored_local_path == child.root.as_posix()
        assert child_ref.get_unique_key() in ctx.callback_downloaded
        materialized = LocalDependencySource(
            ctx, child_ref, child_ref.get_install_path(modules), child_ref.get_unique_key()
        ).acquire()
        assert materialized is not None
        assert materialized.package_info.package.source_path == child.root
        assert (
            child_ref.get_install_path(modules)
            .joinpath(".apm", "prompts", "child.prompt.md")
            .read_bytes()
            == source.read_bytes()
        )
        ctx.downloader.download_package.assert_not_called()


@pytest.mark.req("req-mf-016")
def test_user_multihop_chain_keeps_each_original_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grandchildren resolve from their immediate source parent, never an ancestor."""
    first = LocalPackageFactory(tmp_path / "sources" / "first")
    second = LocalPackageFactory(tmp_path / "sources" / "second")
    grandchild = second.create("grandchild")
    child = second.create("child", dependencies=[{"path": "../grandchild"}])
    parent = first.create("parent", dependencies=[{"path": "../../second/child"}])
    ancestor_decoy = first.create("grandchild", version="9.9.9")
    unrelated = LocalPackageFactory(tmp_path / "unrelated")
    cwd_decoy = unrelated.create("grandchild", version="8.8.8")
    cwd = unrelated.create("cwd")
    monkeypatch.chdir(cwd.root)
    consumer = LocalPackageFactory(tmp_path / "user").create(
        "consumer", dependencies=[{"path": parent.root.as_posix()}]
    )
    with _resolved_scope(consumer.manifest_path, InstallScope.USER) as ctx:
        assert not ctx.callback_failures
        refs = {dep.repo_url: dep for dep in ctx.deps_to_install}
        assert set(refs) == {"_local/parent", "_local/child", "_local/grandchild"}
        for package, declaring, spelling in (
            (child, parent, "../../second/child"),
            (grandchild, child, "../grandchild"),
        ):
            ref = refs[f"_local/{package.name}"]
            node = ctx.dependency_graph.dependency_tree.get_node(ref.get_unique_key())
            assert node.parent.package.source_path == declaring.root
            assert node.package.source_path == package.root
            assert ctx.dep_base_dirs[ref.get_unique_key()] == declaring.root
            assert ref.local_path == spelling
            assert ref.anchored_local_path == package.root.as_posix()
            materialized = LocalDependencySource(
                ctx, ref, ref.get_install_path(ctx.apm_modules_dir), ref.get_unique_key()
            ).acquire()
            assert materialized is not None
            assert materialized.package_info.package.source_path == package.root
            assert (materialized.install_path / "apm.yml").read_bytes() == (
                package.manifest_path.read_bytes()
            )
        assert ancestor_decoy.manifest_path.is_file()
        assert cwd_decoy.manifest_path.is_file()
        assert (
            refs["_local/grandchild"]
            .get_install_path(ctx.apm_modules_dir)
            .joinpath("apm.yml")
            .read_bytes()
            != ancestor_decoy.manifest_path.read_bytes()
        )
        ctx.downloader.download_package.assert_not_called()


@pytest.mark.req("req-mf-016")
def test_missing_user_anchor_does_not_use_other_scope_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded identity and a matching project installation do not authorize replay."""
    factory = LocalPackageFactory(tmp_path / "sources")
    child = factory.create("child")
    parent = factory.create("parent", dependencies=[{"path": "../child"}])
    consumer = LocalPackageFactory(tmp_path / "user").create(
        "consumer", dependencies=[{"path": parent.root.as_posix()}]
    )
    project = LocalPackageFactory(tmp_path / "project").create("consumer")
    monkeypatch.chdir(project.root)
    with _resolved_scope(consumer.manifest_path, InstallScope.USER) as ctx:
        ref = next(dep for dep in ctx.deps_to_install if dep.repo_url == "_local/child")
        project_copy = ref.get_install_path(project.root / "apm_modules")
        shutil.copytree(child.root, project_copy)
        before = (project_copy / "apm.yml").read_bytes()
        destination = ref.get_install_path(ctx.apm_modules_dir)
        shutil.rmtree(destination)
        node = ctx.dependency_graph.dependency_tree.get_node(ref.get_unique_key())
        node.parent.package.source_path = None
        assert ref.declaring_parent and ref.anchored_local_path
        assert ctx.dep_base_dirs[ref.get_unique_key()] == parent.root
        assert (
            user_scope_rejection_reason(ref, InstallScope.USER, parent_pkg=node.parent.package)
            is not None
        )
        result = LocalDependencySource(ctx, ref, destination, ref.get_unique_key()).acquire()
        assert result is None
        assert not destination.exists()
        assert (project_copy / "apm.yml").read_bytes() == before
        assert child.manifest_path.read_bytes() == before
        ctx.downloader.download_package.assert_not_called()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
@pytest.mark.parametrize("repository", ["org/repo", "_local/parent"])
@pytest.mark.parametrize(
    "reference", ["../child", "../../../outside", "absolute"], ids=["sibling", "escape", "absolute"]
)
def test_remote_paths_are_routed_before_local_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    repository: str,
    scope: InstallScope,
) -> None:
    """Parsed Git origin outranks scope and repository spelling before local reads."""
    outside = LocalPackageFactory(tmp_path).create("outside")
    if reference == "absolute":
        reference = outside.root.as_posix()
    factory = LocalPackageFactory(tmp_path / "remote" / "packages")
    parent = factory.create("parent", dependencies=[{"path": reference}])
    child = factory.create("child")
    remote = {
        "git": f"https://gitlab.example.invalid:8443/{repository}",
        "path": "packages/parent",
        "ref": "a" * 40,
    }
    consumer = LocalPackageFactory(tmp_path / "user").create("consumer", dependencies=[remote])
    fixtures = {"packages/parent": parent, "packages/child": child}

    def download(dep: DependencyReference, destination: Path) -> None:
        assert not dep.is_local and dep.local_path is None
        shutil.copytree(fixtures[dep.virtual_path].root, destination)

    downloader = MagicMock(shared_clone_cache=None)
    downloader.download_package.side_effect = download
    local_copy = MagicMock(side_effect=AssertionError("Remote path reached local acquisition"))
    monkeypatch.setattr("apm_cli.install.phases.local_content._copy_local_package", local_copy)
    with _resolved_scope(consumer.manifest_path, scope, downloader) as ctx:
        local_copy.assert_not_called()
        requested = [call.args[0] for call in downloader.download_package.call_args_list]
        expected_paths = ["packages/parent"]
        if reference == "../child":
            expected_paths.append("packages/child")
            assert not ctx.callback_failures
        else:
            assert ctx.callback_failures == {DependencyReference.parse(reference).get_unique_key()}
        assert [dep.virtual_path for dep in requested] == expected_paths
        assert {dep.virtual_path for dep in ctx.deps_to_install} == set(expected_paths)
        original = DependencyReference.parse_from_dict(remote)
        for dep in requested:
            assert (dep.host, dep.port, dep.repo_url, dep.reference, dep.explicit_scheme) == (
                original.host,
                original.port,
                original.repo_url,
                original.reference,
                original.explicit_scheme,
            )
            assert user_scope_rejection_reason(dep, InstallScope.USER) is None
            assert dep.get_unique_key() in ctx.callback_downloaded
            node = ctx.dependency_graph.dependency_tree.get_node(dep.get_unique_key())
            assert node.package.proven_source_kind == "git"
            assert node.package.source_path.is_relative_to(ctx.apm_modules_dir)
            assert dep.get_install_path(ctx.apm_modules_dir).joinpath("apm.yml").read_bytes() == (
                fixtures[dep.virtual_path].manifest_path.read_bytes()
            )
        assert all(not dep.is_local for dep in ctx.deps_to_install)
        assert (
            not DependencyReference.parse(outside.root.as_posix())
            .get_install_path(ctx.apm_modules_dir)
            .exists()
        )
        assert outside.manifest_path.is_file()
        local_copy.assert_not_called()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "context", ["direct", "missing-parent", "missing-source", "relative-source", "remote"]
)
def test_user_relative_admission_requires_proven_local_parent(tmp_path: Path, context: str) -> None:
    """Existing files and a claimed anchor alone cannot authorize a global read."""
    child = LocalPackageFactory(tmp_path).create("child")
    dep = DependencyReference.parse("./child")
    if context != "direct":
        dep.declaring_parent = tmp_path.as_posix()
    parent = APMPackage(
        name="parent",
        version="1.0.0",
        source="org/remote" if context == "remote" else "_local/parent",
        source_path=(
            None
            if context == "missing-source"
            else Path("relative")
            if context == "relative-source"
            else tmp_path
        ),
    )
    reason = user_scope_rejection_reason(
        dep, InstallScope.USER, parent_pkg=None if context == "missing-parent" else parent
    )
    assert child.manifest_path.is_file()
    assert reason is not None
    assert "relative local paths" in reason
    assert "absolute path" in reason
    assert (
        user_scope_rejection_reason(
            DependencyReference.parse(child.root.as_posix()), InstallScope.USER
        )
        is None
    )


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("case", ["project-relative", "user-absolute", "user-home"])
def test_direct_local_source_is_anchored_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Absolute/home sources work globally; project-relative sources use the project."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reference = {
        "project-relative": "../sources/package",
        "user-absolute": package.root.as_posix(),
        "user-home": "~/sources/package",
    }[case]
    scope = InstallScope.PROJECT if case == "project-relative" else InstallScope.USER
    dep = DependencyReference.parse(reference)
    assert user_scope_rejection_reason(dep, scope) is None
    destination = consumer / "apm_modules" / "_local" / "package"
    assert (
        _copy_local_package(dep, destination, consumer, project_root=consumer, logger=None)
        == destination
    )
    assert (destination / "apm.yml").read_bytes() == package.manifest_path.read_bytes()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("reference", ["../child", "..\\child"])
def test_remote_relative_child_retains_repository_and_ref(tmp_path: Path, reference: str) -> None:
    """A sibling inside the remote repository remains remote, not a host-file read."""
    modules = tmp_path / "apm_modules"
    factory = LocalPackageFactory(modules / "repo" / "packages")
    parent = factory.create("parent")
    child = factory.create("child")
    parent_dep = DependencyReference.parse_from_dict(
        {
            "git": "https://gitlab.example.invalid:8443/org/repo",
            "path": "packages/parent",
            "ref": "a" * 40,
        }
    )
    parent_pkg = APMPackage.from_apm_yml(parent.manifest_path, source_path=parent.root)
    parent_pkg.source = parent_dep.repo_url
    resolver = APMDependencyResolver(apm_modules_dir=modules)
    expanded = resolver._expand_or_reject_remote_parent_local_path(
        parent_dep, parent_pkg, DependencyReference.parse(reference)
    )
    assert child.manifest_path.is_file()
    assert expanded is not None
    assert not expanded.is_local
    assert expanded.local_path is None
    assert expanded.virtual_path == "packages/child"
    assert (
        expanded.host,
        expanded.port,
        expanded.repo_url,
        expanded.reference,
        expanded.explicit_scheme,
    ) == (
        parent_dep.host,
        parent_dep.port,
        parent_dep.repo_url,
        parent_dep.reference,
        parent_dep.explicit_scheme,
    )
    assert not resolver._rejected_remote_local_keys


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "kind", ["escape", "absolute", "absolute-inside", "home", "windows-absolute", "symlink"]
)
def test_remote_parent_cannot_expand_into_host_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """The real expansion and loader gates refuse remote-to-host transitions."""
    modules = tmp_path / "apm_modules"
    parent = LocalPackageFactory(modules / "repo" / "packages").create("parent")
    outside = LocalPackageFactory(tmp_path).create("outside")
    parent_dep = DependencyReference.parse_from_dict(
        {"git": "https://gitlab.example.invalid/org/repo", "path": "packages/parent", "ref": "v1"}
    )
    parent_pkg = APMPackage.from_apm_yml(parent.manifest_path, source_path=parent.root)
    parent_pkg.source = parent_dep.repo_url
    paths = {
        "escape": "../../../../outside",
        "absolute": outside.root.as_posix(),
        "absolute-inside": parent.root.as_posix(),
        "home": "~/outside",
        "windows-absolute": "C:/outside",
        "symlink": "../linked",
    }
    if kind == "symlink":
        (parent.root.parent / "linked").symlink_to(outside.root, target_is_directory=True)
    dep = DependencyReference.parse_from_dict({"path": paths[kind]})
    callback = MagicMock(side_effect=AssertionError("Rejected remote path reached acquisition"))
    resolver = APMDependencyResolver(apm_modules_dir=modules, download_callback=callback)
    assert resolver._expand_or_reject_remote_parent_local_path(parent_dep, parent_pkg, dep) is None
    assert dep.get_unique_key() in resolver._rejected_remote_local_keys
    assert resolver._try_load_dependency_package(dep, parent_pkg=parent_pkg) is None
    callback.assert_not_called()
    assert outside.manifest_path.is_file()
    assert not (modules / "_local").exists()
    assert paths[kind] in "".join(capsys.readouterr().out.split())


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "selected_alias", [False, True], ids=["source-directory", "resolved-source-alias"]
)
def test_selected_local_root_allows_only_internal_symlink_content(
    tmp_path: Path, selected_alias: bool
) -> None:
    """Selecting a source-directory alias is distinct from following its contents."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    target = package.root / "content.txt"
    target.write_text("Internal content\n", encoding="ascii")
    (package.root / "link.txt").symlink_to("content.txt")
    selected = package.root
    if selected_alias:
        selected = tmp_path / "chosen-package"
        selected.symlink_to(package.root, target_is_directory=True)
    consumer = tmp_path / "consumer"
    destination = consumer / "apm_modules" / "_local" / "package"
    result = _copy_local_package(
        DependencyReference.parse(selected.as_posix()),
        destination,
        consumer,
        project_root=consumer,
        logger=None,
    )
    assert result == destination
    assert (destination / "link.txt").read_bytes() == target.read_bytes()
    assert not (destination / "link.txt").is_symlink()
    assert (destination / "content.txt").read_bytes() == target.read_bytes()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("kind", ["outside-file", "outside-directory", "broken", "directory-cycle"])
def test_local_package_rejects_uncontained_or_unresolvable_symlink(
    tmp_path: Path, kind: str
) -> None:
    """A selected local source cannot import external or invalid link content."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "not-package-content.txt"
    secret.write_text("Outside content\n", encoding="ascii")
    targets = {
        "outside-file": secret,
        "outside-directory": outside,
        "broken": package.root / "missing",
        "directory-cycle": package.root,
    }
    link = package.root / "linked"
    link.symlink_to(
        targets[kind], target_is_directory=kind in {"outside-directory", "directory-cycle"}
    )
    destination = tmp_path / "consumer" / "apm_modules" / "_local" / "package"
    with pytest.raises(PathTraversalError) as error:
        _copy_local_package(
            DependencyReference.parse(package.root.as_posix()),
            destination,
            tmp_path / "consumer",
            project_root=tmp_path / "consumer",
            logger=None,
        )
    assert "linked" in str(error.value)
    assert not destination.exists()
    assert secret.read_text(encoding="ascii") == "Outside content\n"


@pytest.mark.req("req-mf-016")
def test_local_package_rejects_file_symlink_cycle(tmp_path: Path) -> None:
    """An OS-detected file cycle also fails; no whole-install rollback is promised."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    (package.root / "linked").symlink_to("linked")
    destination = tmp_path / "consumer" / "apm_modules" / "_local" / "package"
    # pathlib reports a cycle as RuntimeError on Python 3.12; newer versions
    # use OSError, which the copier translates to PathTraversalError.
    with pytest.raises((PathTraversalError, RuntimeError), match="linked"):
        _copy_local_package(
            DependencyReference.parse(package.root.as_posix()),
            destination,
            tmp_path / "consumer",
            project_root=tmp_path / "consumer",
            logger=None,
        )
    assert not (destination / "linked").exists()
