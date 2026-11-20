"""Lockfile content-hash streams and the one-time promotion to version 4 (#4000)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from apm_cli.deps.lockfile import (
    LockedDependency,
    LockFile,
    UnsupportedLockfileVersionError,
    require_supported_lockfile_version,
)
from apm_cli.install.phases._redownload import _should_skip_redownload
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.install.source_helpers import record_package_digests
from apm_cli.utils.content_hash import compute_package_digests

pytestmark = pytest.mark.component


def _package(modules: Path, repo: str, payload: bytes) -> Path:
    root = modules.joinpath(*repo.split("/"))
    root.mkdir(parents=True)
    (root / "SKILL.md").write_bytes(payload)
    return root


def _legacy_lock(version: str, rows: dict[str, str]) -> LockFile:
    deps = "".join(
        f"- repo_url: {repo}\n  resolved_commit: {'a' * 40}\n  content_hash: '{digest}'\n"
        for repo, digest in rows.items()
    )
    return LockFile.from_yaml(f"lockfile_version: '{version}'\ndependencies:\n{deps}")


def _ctx(modules: Path, existing: LockFile | None, **flags) -> SimpleNamespace:
    return SimpleNamespace(
        existing_lockfile=existing,
        apm_modules_dir=modules,
        package_hashes={},
        package_framed_hashes={},
        frozen=flags.get("frozen", False),
        dry_run=False,
    )


@pytest.mark.parametrize("version", ["1", "2"])
def test_legacy_version_rows_verify_with_the_legacy_stream(tmp_path: Path, version: str) -> None:
    root = _package(tmp_path, "acme/widgets", b"payload")
    digests = compute_package_digests(root)
    lock = _legacy_lock(version, {"acme/widgets": digests.legacy})
    row = lock.get_dependency("acme/widgets")

    assert lock.framed_content_hashes is False
    assert row.content_hash_framed is False
    assert _should_skip_redownload(row, root) is True
    row.content_hash = digests.framed
    assert _should_skip_redownload(row, root) is False


def test_version_4_round_trips_and_verifies_with_the_framed_stream(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme/widgets", b"payload")
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(
            repo_url="acme/widgets",
            resolved_commit="a" * 40,
            content_hash=compute_package_digests(root).framed,
        )
    )

    restored = LockFile.from_yaml(lock.to_yaml())

    assert yaml.safe_load(lock.to_yaml())["lockfile_version"] == "4"
    assert restored.get_dependency("acme/widgets").content_hash_framed is True
    assert _should_skip_redownload(restored.get_dependency("acme/widgets"), root) is True


def test_a_release_without_version_4_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A v0.36.1-equivalent version check refuses a version 4 lockfile."""
    import apm_cli.deps.lockfile as lockfile_module

    lock = LockFile()
    lock.add_dependency(
        LockedDependency(repo_url="acme/widgets", content_hash="sha256:" + "0" * 64)
    )
    data = yaml.safe_load(lock.to_yaml())
    monkeypatch.setattr(lockfile_module, "SUPPORTED_LOCKFILE_VERSIONS", frozenset({"1", "2", "3"}))

    with pytest.raises(UnsupportedLockfileVersionError, match="'4'"):
        require_supported_lockfile_version(data)


def test_writable_install_promotes_every_row_from_verified_bytes(tmp_path: Path) -> None:
    recorded = _package(tmp_path, "acme/widgets", b"fresh")
    carried = _package(tmp_path, "acme/gadgets", b"cached")
    recorded_digests = compute_package_digests(recorded)
    carried_digests = compute_package_digests(carried)
    existing = _legacy_lock(
        "1", {"acme/widgets": recorded_digests.legacy, "acme/gadgets": carried_digests.legacy}
    )
    ctx = _ctx(tmp_path, existing)
    record_package_digests(ctx, "acme/widgets", recorded_digests)
    assert ctx.package_hashes["acme/widgets"] == recorded_digests.legacy
    lock = LockFile.from_yaml(existing.to_yaml())

    LockfileBuilder(ctx)._promote_content_hashes(lock)

    assert lock.get_dependency("acme/widgets").content_hash == recorded_digests.framed
    assert lock.get_dependency("acme/gadgets").content_hash == carried_digests.framed
    assert yaml.safe_load(lock.to_yaml())["lockfile_version"] == "4"


def test_unverified_carried_row_blocks_promotion_and_keeps_every_row(tmp_path: Path) -> None:
    recorded = _package(tmp_path, "acme/widgets", b"fresh")
    carried = _package(tmp_path, "acme/gadgets", b"cached")
    recorded_digests = compute_package_digests(recorded)
    existing = _legacy_lock(
        "1",
        {
            "acme/widgets": recorded_digests.legacy,
            "acme/gadgets": compute_package_digests(carried).legacy,
        },
    )
    (carried / "SKILL.md").write_bytes(b"edited after install")
    ctx = _ctx(tmp_path, existing)
    record_package_digests(ctx, "acme/widgets", recorded_digests)
    lock = LockFile.from_yaml(existing.to_yaml())
    before = lock.to_yaml()

    LockfileBuilder(ctx)._promote_content_hashes(lock)

    assert lock.framed_content_hashes is False
    assert lock.to_yaml() == before
    assert yaml.safe_load(before)["lockfile_version"] == "1"


def test_frozen_install_never_promotes(tmp_path: Path) -> None:
    root = _package(tmp_path, "acme/widgets", b"payload")
    digests = compute_package_digests(root)
    existing = _legacy_lock("1", {"acme/widgets": digests.legacy})
    ctx = _ctx(tmp_path, existing, frozen=True)
    record_package_digests(ctx, "acme/widgets", digests)
    lock = LockFile.from_yaml(existing.to_yaml())
    before = lock.to_yaml()

    LockfileBuilder(ctx)._promote_content_hashes(lock)

    assert lock.to_yaml() == before
    assert lock.get_dependency("acme/widgets").content_hash == digests.legacy
