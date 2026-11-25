"""Lockfile reads stay faithful to the file on disk across writes and mutations."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-read-only-lockfile-path")]

_HASH = "sha256:" + "1" * 64


def test_read_after_write_returns_new_data(tmp_path: Path) -> None:
    lock_path = tmp_path / "apm.lock.yaml"
    first = LockFile()
    first.add_dependency(LockedDependency(repo_url="owner/first", resolved_commit="a" * 40))
    first.write(lock_path)
    assert LockFile.read(lock_path).has_dependency("owner/first")

    second = LockFile()
    second.add_dependency(LockedDependency(repo_url="owner/second", resolved_commit="b" * 40))
    second.write(lock_path)

    on_disk = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert [row["repo_url"] for row in on_disk["dependencies"]] == ["owner/second"]
    reread = LockFile.read(lock_path)
    assert reread.has_dependency("owner/second")
    assert not reread.has_dependency("owner/first")


def test_mutating_a_read_lockfile_does_not_change_the_next_read(tmp_path: Path) -> None:
    lock_path = tmp_path / "apm.lock.yaml"
    seed = LockFile()
    seed.add_dependency(
        LockedDependency(
            repo_url="owner/repo",
            resolved_commit="a" * 40,
            deployed_files=[".github/a.md"],
            deployed_file_hashes={".github/a.md": _HASH},
        )
    )
    original = seed.to_yaml()
    lock_path.write_text(original, encoding="utf-8")

    mutated = LockFile.read(lock_path)
    dep = mutated.get_dependency("owner/repo")
    dep.deployed_files.append(".github/injected.md")
    dep.deployed_file_hashes[".github/a.md"] = "sha256:" + "2" * 64
    mutated.mcp_servers.append("injected")
    mutated.dependencies.clear()

    fresh = LockFile.read(lock_path).get_dependency("owner/repo")
    assert fresh.deployed_files == [".github/a.md"]
    assert fresh.deployed_file_hashes == {".github/a.md": _HASH}
    assert LockFile.read(lock_path).mcp_servers == []
    assert lock_path.read_text(encoding="utf-8") == original
