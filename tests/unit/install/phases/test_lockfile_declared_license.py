"""Tests for LockfileBuilder._attach_declared_licenses (issue #1777, U6).

Verifies the declared-license provenance captured at acquire time is attached
to the right lockfile entries, and that deps which declared nothing keep
``declared_license`` as ``None`` (so the entry OMITS it == unknown).
"""

from __future__ import annotations

from types import SimpleNamespace

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder


def test_attaches_declared_license_to_matching_entry():
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="owner/pkg"))
    builder = LockfileBuilder(SimpleNamespace(package_declared_licenses={"owner/pkg": "MIT"}))

    builder._attach_declared_licenses(lockfile)

    assert lockfile.dependencies["owner/pkg"].declared_license == "MIT"


def test_undeclared_dep_keeps_none():
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="owner/pkg"))
    builder = LockfileBuilder(SimpleNamespace(package_declared_licenses={}))

    builder._attach_declared_licenses(lockfile)

    assert lockfile.dependencies["owner/pkg"].declared_license is None


def test_unknown_dep_key_is_ignored():
    lockfile = LockFile()
    lockfile.add_dependency(LockedDependency(repo_url="owner/pkg"))
    builder = LockfileBuilder(SimpleNamespace(package_declared_licenses={"ghost/dep": "MIT"}))

    builder._attach_declared_licenses(lockfile)

    assert lockfile.dependencies["owner/pkg"].declared_license is None
