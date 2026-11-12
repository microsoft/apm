"""Declared Git transport for lock-pinned scratch replay (#3374)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.replay_transport import DeclaredTransportIndex
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml

_SHA = "a" * 40


def _locked(repo_url: str, **fields: object) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        host="github.com",
        resolved_commit=_SHA,
        resolved_ref=_SHA,
        **fields,
    )


def _apply(index: DeclaredTransportIndex, lock_dep: LockedDependency) -> DependencyReference:
    return index.apply(lock_dep, lock_dep.to_dependency_ref())


def test_root_scp_declaration_replays_over_ssh() -> None:
    index = DeclaredTransportIndex()
    index.record([DependencyReference.parse("git@github.com:acme/pkg.git")])

    replayed = _apply(index, _locked("acme/pkg"))

    assert replayed.explicit_scheme == "ssh"
    assert replayed.ssh_user == "git"


def test_ssh_url_declaration_carries_user_and_lock_port() -> None:
    index = DeclaredTransportIndex()
    index.record([DependencyReference.parse("ssh://deploy@git.example.com:7999/acme/pkg.git")])
    lock_dep = LockedDependency(
        repo_url="acme/pkg",
        host="git.example.com",
        port=7999,
        resolved_commit=_SHA,
    )

    replayed = _apply(index, lock_dep)

    assert (replayed.explicit_scheme, replayed.ssh_user, replayed.port) == ("ssh", "deploy", 7999)


def test_https_and_shorthand_declarations_keep_their_selection() -> None:
    index = DeclaredTransportIndex()
    index.record(
        [
            DependencyReference.parse("https://github.com/acme/web.git"),
            DependencyReference.parse("acme/short"),
        ]
    )

    assert _apply(index, _locked("acme/web")).explicit_scheme == "https"
    assert _apply(index, _locked("acme/short")).explicit_scheme is None


def test_undeclared_lock_entry_is_unchanged() -> None:
    lock_dep = _locked("acme/other")
    original = lock_dep.to_dependency_ref()

    assert DeclaredTransportIndex().apply(lock_dep, original) is original


def test_first_declaration_wins() -> None:
    index = DeclaredTransportIndex()
    index.record([DependencyReference.parse("git@github.com:acme/pkg.git")])
    index.record([DependencyReference.parse("https://github.com/acme/pkg.git")])

    assert _apply(index, _locked("acme/pkg")).explicit_scheme == "ssh"


def test_replacement_rows_keep_their_projected_source() -> None:
    index = DeclaredTransportIndex()
    index.record([DependencyReference.parse("git@github.com:acme/pkg.git")])
    lock_dep = _locked("acme/pkg")
    lock_dep.replacement = MagicMock()
    dep_ref = DependencyReference.parse("acme/pkg")

    assert index.apply(lock_dep, dep_ref) is dep_ref


def _write_manifest(root: Path, apm: list[object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {"name": root.name, "version": "1.0.0", "dependencies": {"apm": apm}},
        root / "apm.yml",
    )


def _materialize(
    index: DeclaredTransportIndex, modules: Path, lock_dep: LockedDependency, apm: list[object]
) -> None:
    package_root = lock_dep.to_dependency_ref().get_install_path(modules)
    _write_manifest(package_root, apm)
    index.record_package(package_root, lock_dep)


def test_transitive_declaration_comes_from_materialized_parent(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    index = DeclaredTransportIndex(modules_dir=modules)
    index.record([DependencyReference.parse("git@github.com:acme/parent.git")])
    _materialize(
        index,
        modules,
        _locked("acme/parent"),
        ["git@github.com:acme/child.git", {"git": "parent", "path": "packages/sibling"}],
    )

    assert _apply(index, _locked("acme/child", depth=2)).explicit_scheme == "ssh"
    sibling = _locked("acme/parent", depth=2, is_virtual=True, virtual_path="packages/sibling")
    assert _apply(index, sibling).explicit_scheme == "ssh"


def test_shared_child_follows_install_admission_order_not_lock_order(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    index = DeclaredTransportIndex(modules_dir=modules)
    index.record(
        [DependencyReference.parse("acme/z-parent"), DependencyReference.parse("acme/a-parent")]
    )
    # Replay walks the lock in (depth, repo_url) order: a-parent first.
    _materialize(index, modules, _locked("acme/a-parent"), ["https://github.com/acme/child.git"])
    _materialize(index, modules, _locked("acme/z-parent"), ["git@github.com:acme/child.git"])

    assert _apply(index, _locked("acme/child", depth=2)).explicit_scheme == "ssh"


def test_remote_relative_path_child_inherits_parent_ssh(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    index = DeclaredTransportIndex(modules_dir=modules)
    index.record(
        [
            DependencyReference.parse_from_dict(
                {"git": "ssh://deploy@github.com/acme/mono.git", "path": "packages/parent"}
            )
        ]
    )
    parent = _locked("acme/mono", is_virtual=True, virtual_path="packages/parent")
    _materialize(index, modules, parent, [{"path": "../shared"}])

    shared = _locked("acme/mono", depth=2, is_virtual=True, virtual_path="packages/shared")
    replayed = _apply(index, shared)

    assert (replayed.explicit_scheme, replayed.ssh_user) == ("ssh", "deploy")


def test_root_shorthand_keeps_default_transport_over_later_ssh(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    index = DeclaredTransportIndex(modules_dir=modules)
    index.record(
        [DependencyReference.parse("acme/a-parent"), DependencyReference.parse("acme/z-shared")]
    )
    _materialize(index, modules, _locked("acme/a-parent"), ["git@github.com:acme/z-shared.git#v1"])

    assert _apply(index, _locked("acme/z-shared")).explicit_scheme is None


def test_package_without_manifest_records_nothing(tmp_path: Path) -> None:
    index = DeclaredTransportIndex(modules_dir=tmp_path)
    index.record([DependencyReference.parse("acme/parent")])
    index.record_package(tmp_path / "missing", _locked("acme/parent"))

    assert _apply(index, _locked("acme/child", depth=2)).explicit_scheme is None


def test_materialize_hands_declared_ssh_to_downloader(tmp_path: Path) -> None:
    from apm_cli.install.drift import _materialize_install_path

    index = DeclaredTransportIndex()
    index.record([DependencyReference.parse("git@github.com:acme/pkg.git")])
    lock_dep = _locked("acme/pkg")
    lockfile = LockFile()
    lockfile.add_dependency(lock_dep)
    downloader = MagicMock()
    downloader.download_package.return_value = MagicMock(resolved_reference=None)

    _materialize_install_path(
        lock_dep,
        tmp_path / "project",
        tmp_path / "scratch" / "apm_modules",
        cache_only=False,
        lockfile=lockfile,
        downloader=downloader,
        declared_transports=index,
    )

    download_ref = downloader.download_package.call_args.args[0]
    assert download_ref.explicit_scheme == "ssh"
    assert download_ref.reference == _SHA
