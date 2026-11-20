"""Lockfile rewrites never lower lockfile_version below 2 once written (req-lk-002)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("req-lk-002")]


def _git_dep(**kwargs) -> LockedDependency:
    defaults = dict(
        repo_url="acme/foo",
        host="github.com",
        resolved_commit="abc123",
        resolved_ref="v1.0",
    )
    defaults.update(kwargs)
    return LockedDependency(**defaults)


def _registry_dep() -> LockedDependency:
    return LockedDependency(
        repo_url="acme/web",
        host="github.com",
        source="registry",
        version="1.2.0",
        resolved_url="https://reg.example.com/apm/v1/packages/acme/web/versions/1.2.0/tarball",
        resolved_hash="sha256:" + "a" * 64,
    )


@pytest.mark.parametrize(
    "v2_dep",
    [_registry_dep, lambda: _git_dep(repo_url="acme/semver", constraint="^1.0.0")],
    ids=["registry", "git-semver"],
)
def test_rewrite_keeps_v2_after_the_v2_entry_is_removed(tmp_path: Path, v2_dep):
    """req-lk-002: a rewrite of a v2 lockfile never demotes it to v1."""
    path = tmp_path / "apm.lock.yaml"
    first = LockFile()
    first.add_dependency(_git_dep())
    first.add_dependency(v2_dep())
    first.write(path)
    assert "lockfile_version: '2'" in path.read_text(encoding="utf-8")

    rewrite = LockFile()
    rewrite.add_dependency(_git_dep())
    rewrite.write(path)

    assert "lockfile_version: '2'" in path.read_text(encoding="utf-8")
    assert LockFile.read(path).lockfile_version == "2"


def test_v4_marker_follows_framed_content_hashes_not_the_floor(tmp_path: Path):
    """Versions 3 and 4 follow their own rules; the floor only rules out version 1."""
    path = tmp_path / "apm.lock.yaml"
    first = LockFile()
    first.add_dependency(_git_dep(content_hash="sha256:" + "b" * 64))
    first.write(path)
    assert "lockfile_version: '4'" in path.read_text(encoding="utf-8")

    rewrite = LockFile()
    rewrite.add_dependency(_git_dep())
    rewrite.write(path)

    assert "lockfile_version: '2'" in path.read_text(encoding="utf-8")


def test_new_lockfile_without_v2_entries_is_v1(tmp_path: Path):
    path = tmp_path / "apm.lock.yaml"
    lock = LockFile()
    lock.add_dependency(_git_dep())
    lock.write(path)
    assert "lockfile_version: '1'" in path.read_text(encoding="utf-8")
