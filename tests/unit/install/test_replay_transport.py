"""Declared Git transport for lock-pinned scratch replay (#3374)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.install.replay_transport import DeclaredTransportIndex
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit

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
