"""Unit tests for the cache-pin marker module.

These tests pin the supply-chain hardening contract (PR #1137 follow-up):
``apm install`` writes a ``.apm-pin`` marker into each cached package
root, and ``apm audit`` verifies the marker matches the lockfile's
``resolved_commit`` before running drift replay.

Coverage:
  * Positive: matching marker passes verification.
  * Missing: absent marker -> CachePinError.
  * Malformed JSON: garbled marker -> CachePinError.
  * Wrong schema_version: future-incompatible marker -> CachePinError.
  * Missing resolved_commit field -> CachePinError.
  * Mismatched commit -> CachePinError.
  * sync_markers_for_lockfile skips local deps and deps without commits.
  * sync_markers_for_lockfile is idempotent (re-runs overwrite cleanly).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.cache_pin import (
    MARKER_FILENAME,
    SCHEMA_VERSION,
    CachePinError,
    sync_markers_for_lockfile,
    verify_marker,
    write_marker,
)


def _make_install_dir(tmp_path: Path, name: str = "pkg") -> Path:
    install = tmp_path / name
    install.mkdir(parents=True)
    return install


def test_write_then_verify_matching_commit_passes(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    write_marker(install, "deadbeef" * 5)
    # Should NOT raise.
    verify_marker(install, "deadbeef" * 5)


def test_verify_missing_marker_raises_with_actionable_message(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)  # no marker written
    with pytest.raises(CachePinError) as exc_info:
        verify_marker(install, "deadbeef" * 5)
    msg = str(exc_info.value)
    assert "missing" in msg.lower()
    assert MARKER_FILENAME in msg


def test_verify_malformed_json_raises(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    (install / MARKER_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(CachePinError) as exc_info:
        verify_marker(install, "deadbeef" * 5)
    assert "JSON" in str(exc_info.value) or "json" in str(exc_info.value)


def test_verify_non_object_payload_raises(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    (install / MARKER_FILENAME).write_text('"a string"', encoding="utf-8")
    with pytest.raises(CachePinError) as exc_info:
        verify_marker(install, "deadbeef" * 5)
    assert "object" in str(exc_info.value).lower()


def test_verify_unsupported_schema_version_raises(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    payload = {"schema_version": 999, "resolved_commit": "deadbeef" * 5}
    (install / MARKER_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CachePinError) as exc_info:
        verify_marker(install, "deadbeef" * 5)
    assert "schema_version" in str(exc_info.value)


def test_verify_missing_resolved_commit_field_raises(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    payload = {"schema_version": SCHEMA_VERSION}
    (install / MARKER_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CachePinError) as exc_info:
        verify_marker(install, "deadbeef" * 5)
    assert "resolved_commit" in str(exc_info.value)


def test_verify_commit_mismatch_raises_with_both_values(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    write_marker(install, "aaaa" * 10)
    with pytest.raises(CachePinError) as exc_info:
        verify_marker(install, "bbbb" * 10)
    msg = str(exc_info.value)
    assert "mismatch" in msg.lower()
    assert "aaaa" * 10 in msg
    assert "bbbb" * 10 in msg


def test_write_marker_is_idempotent(tmp_path: Path) -> None:
    install = _make_install_dir(tmp_path)
    write_marker(install, "first" * 8)
    write_marker(install, "second" * 7)
    verify_marker(install, "second" * 7)


def test_write_marker_silently_skips_missing_dir(tmp_path: Path) -> None:
    # Should not raise even if the install dir does not exist (cache wiped).
    write_marker(tmp_path / "does-not-exist", "deadbeef" * 5)


def test_sync_markers_writes_for_remote_deps_with_commits(tmp_path: Path) -> None:
    apm_modules = tmp_path / "apm_modules"
    pkg_dir = apm_modules / "owner" / "repo"
    pkg_dir.mkdir(parents=True)

    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        host="github.com",
        resolved_commit="cafebabe" * 5,
        package_type="apm_package",
    )
    lockfile.add_dependency(dep)

    written = sync_markers_for_lockfile(lockfile, tmp_path, apm_modules)
    assert written == 1
    verify_marker(pkg_dir, "cafebabe" * 5)


def test_sync_markers_skips_local_deps(tmp_path: Path) -> None:
    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()

    lockfile = LockFile()
    local_dep = LockedDependency(
        repo_url="./local-pkg",
        source="local",
        local_path="./local-pkg",
        resolved_commit=None,
    )
    lockfile.add_dependency(local_dep)

    written = sync_markers_for_lockfile(lockfile, tmp_path, apm_modules)
    assert written == 0


def test_sync_markers_warns_on_remote_unpinned_dep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Remote dep without ``resolved_commit`` MUST emit a stderr warning.

    Silent no-op was a supply-chain fail-open: an unpinned remote dep
    cannot get a marker, so drift cannot verify its cache freshness.
    Per supply-chain panel feedback (PR #1137), surface this loudly so
    operators notice and re-pin. Marker count is still zero (nothing
    to write), but the warning is mandatory.
    """
    apm_modules = tmp_path / "apm_modules"
    pkg_dir = apm_modules / "owner" / "repo"
    pkg_dir.mkdir(parents=True)

    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        host="github.com",
        resolved_commit=None,  # branch-tracked dep without pin
    )
    lockfile.add_dependency(dep)

    written = sync_markers_for_lockfile(lockfile, tmp_path, apm_modules)
    assert written == 0
    assert not (pkg_dir / MARKER_FILENAME).exists()

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "owner/repo" in combined
    assert "no resolved_commit" in combined
    assert "apm install" in combined


def test_find_unpinned_remote_deps_excludes_local_and_pinned(tmp_path: Path) -> None:
    """find_unpinned_remote_deps returns only remote deps with no commit."""
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="./local-pkg",
            source="local",
            local_path="./local-pkg",
            resolved_commit=None,
        )
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="org/pinned",
            host="github.com",
            resolved_commit="cafebabe" * 5,
        )
    )
    lockfile.add_dependency(
        LockedDependency(
            repo_url="org/unpinned",
            host="github.com",
            resolved_commit=None,
        )
    )

    from apm_cli.install.cache_pin import find_unpinned_remote_deps

    unpinned = find_unpinned_remote_deps(lockfile)
    assert unpinned == ["org/unpinned"]


def test_sync_markers_skips_deps_with_no_cached_install(tmp_path: Path) -> None:
    """A lockfile entry whose cache was wiped MUST NOT crash sync."""
    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()
    # Note: NO pkg_dir created -- cache is empty.

    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        host="github.com",
        resolved_commit="cafebabe" * 5,
    )
    lockfile.add_dependency(dep)

    written = sync_markers_for_lockfile(lockfile, tmp_path, apm_modules)
    assert written == 0


def test_sync_markers_self_heals_caches_missing_marker(tmp_path: Path) -> None:
    """Regression: an existing cache without a marker MUST get marked.

    This is the unchanged-lockfile self-heal scenario: a user upgraded
    APM, ran drift, hit a CachePinError, then ran ``apm install``.
    The lockfile YAML did not change (already current), but
    ``LockfileBuilder`` must still call sync_markers so the cache is
    marked and the next drift run succeeds.
    """
    apm_modules = tmp_path / "apm_modules"
    pkg_dir = apm_modules / "owner" / "repo"
    pkg_dir.mkdir(parents=True)
    # Simulate a pre-existing cache: contents present, but NO marker.
    (pkg_dir / "some-file.md").write_text("cached", encoding="utf-8")
    assert not (pkg_dir / MARKER_FILENAME).exists()

    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        host="github.com",
        resolved_commit="cafebabe" * 5,
    )
    lockfile.add_dependency(dep)

    written = sync_markers_for_lockfile(lockfile, tmp_path, apm_modules)
    assert written == 1
    verify_marker(pkg_dir, "cafebabe" * 5)
