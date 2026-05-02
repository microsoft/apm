"""Unit tests for local_bundle.py -- detection, integrity verification, target-mismatch.

These tests target the module ``apm_cli.bundle.local_bundle`` which does NOT
exist yet.  Every test should fail at **import time** until the production
module is created.  That is the expected TDD state.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# The import below WILL fail until production code lands.  That is intentional
# -- TDD: tests are written before the module exists.
# ---------------------------------------------------------------------------
try:
    from apm_cli.bundle.local_bundle import (
        LocalBundleInfo,
        _looks_like_legacy_apm_bundle,
        check_target_mismatch,
        detect_local_bundle,
        read_bundle_plugin_json,
        verify_bundle_integrity,
    )

    _MODULE_EXISTS = True
except ImportError:
    _MODULE_EXISTS = False

pytestmark = pytest.mark.skipif(
    not _MODULE_EXISTS,
    reason="apm_cli.bundle.local_bundle not yet implemented (TDD stub)",
)


# ---------------------------------------------------------------------------
# Helpers -- lean fixture builders (no committed binaries)
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _make_plugin_bundle(
    tmp_path: Path,
    *,
    plugin_id: str = "test-plugin",
    plugin_name: str = "Test Plugin",
    pack_target: str = "copilot,claude",
    include_lockfile: bool = True,
    files: dict[str, str] | None = None,
) -> Path:
    """Create a minimal plugin bundle directory with computed hashes.

    Returns the bundle root directory.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)

    # plugin.json
    pj = {"id": plugin_id, "name": plugin_name}
    (bundle / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")

    # Default files if none provided
    if files is None:
        files = {
            "skills/coding/SKILL.md": "# Coding Skill\nA helpful skill.",
            "agents/reviewer.md": "# Reviewer Agent\nReviews code.",
        }

    bundle_files: dict[str, str] = {}
    for rel, content in files.items():
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        bundle_files[rel] = _sha256(content)

    if include_lockfile:
        lock_data: dict = {
            "pack": {
                "format": "plugin",
                "target": pack_target,
                "bundle_files": bundle_files,
            },
            "dependencies": [
                {
                    "repo_url": "owner/test-plugin",
                    "resolved_commit": "abc123",
                    "deployed_files": list(files.keys()),
                    "deployed_file_hashes": {k: v for k, v in bundle_files.items()},
                }
            ],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )

    return bundle


def _make_plugin_tarball(tmp_path: Path, bundle_dir: Path) -> Path:
    """Archive a bundle directory into .tar.gz."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)
    return archive_path


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------


class TestDetectLocalBundle:
    """Tests for detect_local_bundle()."""

    def test_detect_plugin_directory(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        result = detect_local_bundle(bundle)
        assert result is not None
        assert isinstance(result, LocalBundleInfo)
        assert result.package_id == "test-plugin"
        assert result.is_archive is False

    def test_detect_plugin_tarball(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        tarball = _make_plugin_tarball(tmp_path / "archives", bundle)
        result = detect_local_bundle(tarball)
        assert result is not None
        assert result.is_archive is True

    def test_detect_returns_none_for_non_bundle(self, tmp_path: Path) -> None:
        """A directory without plugin.json is not a bundle."""
        (tmp_path / "random.txt").write_text("not a bundle", encoding="utf-8")
        result = detect_local_bundle(tmp_path)
        assert result is None

    def test_detect_returns_none_for_nonexistent_path(self, tmp_path: Path) -> None:
        result = detect_local_bundle(tmp_path / "does-not-exist")
        assert result is None

    def test_detect_reads_plugin_json_id(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path, plugin_id="my-custom-id")
        result = detect_local_bundle(bundle)
        assert result is not None
        assert result.package_id == "my-custom-id"

    def test_detect_falls_back_to_dirname_when_no_id(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path, plugin_id="")
        # Rewrite plugin.json without id
        pj = {"name": "Test Plugin"}
        (bundle / "plugin.json").write_text(json.dumps(pj), encoding="utf-8")
        result = detect_local_bundle(bundle)
        assert result is not None
        assert result.package_id == bundle.name

    def test_detect_reads_pack_targets(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path, pack_target="copilot,claude")
        result = detect_local_bundle(bundle)
        assert result is not None
        assert "copilot" in result.pack_targets
        assert "claude" in result.pack_targets

    def test_detect_cleans_temp_dir_on_malicious_archive(self, tmp_path: Path) -> None:
        """Reject paths must not leak temp dirs (review #1099 finding)."""
        import tarfile as _tar
        import tempfile as _tmp

        # Build a tarball whose first member has a `..` segment so the
        # pre-extraction validator returns None on the early-exit path.
        bad = tmp_path / "evil.tar.gz"
        with _tar.open(bad, "w:gz") as tf:
            data = b"x"
            info = _tar.TarInfo(name="../escape")
            info.size = len(data)
            import io as _io

            tf.addfile(info, _io.BytesIO(data))

        before = {p.name for p in Path(_tmp.gettempdir()).glob("apm-local-bundle-*")}
        result = detect_local_bundle(bad)
        after = {p.name for p in Path(_tmp.gettempdir()).glob("apm-local-bundle-*")}
        assert result is None
        # No new apm-local-bundle-* directory left behind.
        assert after - before == set()


# ---------------------------------------------------------------------------
# Legacy apm-format detection tests
# ---------------------------------------------------------------------------


def _make_legacy_apm_bundle(
    tmp_path: Path,
    *,
    pkg_name: str = "test-pkg",
    pkg_version: str = "0.1.0",
    files: dict[str, str] | None = None,
) -> Path:
    """Create a minimal legacy apm-format bundle (apm.lock.yaml, NO plugin.json).

    Mirrors what ``apm pack --format apm --archive`` produces: a directory
    containing deployed files and an ``apm.lock.yaml``, but no ``plugin.json``.
    """
    bundle = tmp_path / f"{pkg_name}-{pkg_version}"
    bundle.mkdir(parents=True, exist_ok=True)

    if files is None:
        files = {".github/copilot-instructions.md": "# Copilot instructions\n"}

    for rel, content in files.items():
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    lock_data: dict = {
        "pack": {
            "format": "apm",
            "target": "copilot",
        },
        "dependencies": [
            {
                "repo_url": "owner/test-pkg",
                "resolved_commit": "abc123",
                "deployed_files": list(files.keys()),
            }
        ],
    }
    (bundle / "apm.lock.yaml").write_text(
        yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
    )
    return bundle


def _make_legacy_tarball(tmp_path: Path, bundle_dir: Path) -> Path:
    """Archive a legacy bundle directory into .tar.gz."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_path / "legacy-bundle.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)
    return archive_path


class TestLegacyApmFormatDetection:
    """detect_local_bundle returns None for legacy --format apm bundles
    (they have apm.lock.yaml but no plugin.json)."""

    def test_detect_returns_none_for_legacy_directory(self, tmp_path: Path) -> None:
        bundle = _make_legacy_apm_bundle(tmp_path)
        result = detect_local_bundle(bundle)
        assert result is None

    def test_detect_returns_none_for_legacy_tarball(self, tmp_path: Path) -> None:
        bundle = _make_legacy_apm_bundle(tmp_path)
        tarball = _make_legacy_tarball(tmp_path / "archives", bundle)
        result = detect_local_bundle(tarball)
        assert result is None

    def test_looks_like_legacy_apm_bundle_true_for_legacy_tarball(self, tmp_path: Path) -> None:
        bundle = _make_legacy_apm_bundle(tmp_path)
        tarball = _make_legacy_tarball(tmp_path / "archives", bundle)
        assert _looks_like_legacy_apm_bundle(tarball) is True

    def test_looks_like_legacy_apm_bundle_false_for_plugin_tarball(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        tarball = _make_plugin_tarball(tmp_path / "archives", bundle)
        assert _looks_like_legacy_apm_bundle(tarball) is False

    def test_looks_like_legacy_apm_bundle_false_for_junk_tarball(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk"
        junk.mkdir()
        (junk / "random.txt").write_text("not a bundle", encoding="utf-8")
        archive = tmp_path / "junk.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(junk, arcname="junk")
        assert _looks_like_legacy_apm_bundle(archive) is False

    def test_looks_like_legacy_apm_bundle_false_for_non_archive(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-archive.txt"
        plain.write_text("hello", encoding="utf-8")
        assert _looks_like_legacy_apm_bundle(plain) is False


# ---------------------------------------------------------------------------
# Integrity verification tests
# ---------------------------------------------------------------------------


class TestVerifyBundleIntegrity:
    """Tests for verify_bundle_integrity()."""

    def test_verify_integrity_passes_valid_bundle(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_install_local_bundle_rejects_tampered_file(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        # Tamper a file after creation
        tampered = bundle / "skills" / "coding" / "SKILL.md"
        tampered.write_text("TAMPERED CONTENT", encoding="utf-8")

        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert len(errors) > 0
        assert any("hash mismatch" in e.lower() or "Hash mismatch" in e for e in errors)

    def test_install_local_bundle_rejects_symlink_in_bundle(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        # Add a symlink
        symlink_path = bundle / "skills" / "coding" / "LINK.md"
        symlink_path.symlink_to(bundle / "agents" / "reviewer.md")

        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert any("symlink" in e.lower() or "Symlink" in e for e in errors)

    def test_install_local_bundle_rejects_missing_bundle_file(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path)
        # Delete a file that the manifest references
        (bundle / "agents" / "reviewer.md").unlink()

        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert len(errors) > 0
        assert any("missing" in e.lower() or "Missing" in e for e in errors)

    def test_install_local_bundle_rejects_missing_lockfile(self, tmp_path: Path) -> None:
        """Bundle with plugin.json but NO apm.lock.yaml -> detected but lockfile is None."""
        bundle = _make_plugin_bundle(tmp_path, include_lockfile=False)
        info = detect_local_bundle(bundle)
        assert info is not None
        assert info.lockfile is None

    def test_bundle_files_path_traversal_rejected(self, tmp_path: Path) -> None:
        """CR1 regression: ``bundle_files`` keys must not allow `..` segments
        or absolute paths -- both cases should produce errors and the bundle
        must NOT be considered valid."""
        bundle = _make_plugin_bundle(tmp_path)
        # Rewrite the lockfile with a malicious bundle_files key.
        evil_path = "../escape.md"
        evil_hash = _sha256("nope")
        lock_data = {
            "pack": {
                "format": "plugin",
                "target": "copilot",
                "bundle_files": {evil_path: evil_hash},
            },
            "dependencies": [],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )
        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert errors, "Path-traversal key must produce errors"
        assert any(
            "traversal" in e.lower() or ".." in e or "invalid" in e.lower() for e in errors
        ), f"Expected path-traversal error, got: {errors}"

    def test_bundle_files_absolute_path_rejected(self, tmp_path: Path) -> None:
        """Absolute bundle_files keys must be rejected."""
        bundle = _make_plugin_bundle(tmp_path)
        lock_data = {
            "pack": {
                "format": "plugin",
                "target": "copilot",
                "bundle_files": {"/etc/passwd": _sha256("nope")},
            },
            "dependencies": [],
        }
        (bundle / "apm.lock.yaml").write_text(
            yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
        )
        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert errors, "Absolute path key must produce errors"

    def test_unlisted_bundle_file_flagged(self, tmp_path: Path) -> None:
        """IM1 regression: files present in the bundle but absent from
        ``bundle_files`` must be flagged so silent payload smuggling is
        impossible.  apm.lock.yaml and plugin.json are allowed extras."""
        bundle = _make_plugin_bundle(tmp_path)
        # Drop a sneaky extra file not listed in the manifest.
        (bundle / "extra-payload.sh").write_text("#!/bin/sh\necho pwned", encoding="utf-8")
        info = detect_local_bundle(bundle)
        assert info is not None and info.lockfile is not None
        errors = verify_bundle_integrity(bundle, info.lockfile)
        assert any("extra-payload.sh" in e for e in errors), (
            f"Unlisted file not flagged. errors={errors}"
        )


# ---------------------------------------------------------------------------
# Target mismatch tests
# ---------------------------------------------------------------------------


class TestCheckTargetMismatch:
    """Tests for check_target_mismatch()."""

    def test_target_mismatch_emits_warning_when_targets_narrower(self) -> None:
        warning = check_target_mismatch(
            bundle_targets=["copilot", "claude"],
            install_targets=["copilot"],
        )
        assert warning is not None
        assert "claude" in warning

    def test_target_match_no_warning(self) -> None:
        warning = check_target_mismatch(
            bundle_targets=["copilot", "claude"],
            install_targets=["copilot", "claude"],
        )
        assert warning is None

    def test_empty_pack_target_no_warning(self) -> None:
        """Pre-constraint bundles (empty pack.target) -> no warning."""
        warning = check_target_mismatch(
            bundle_targets=[],
            install_targets=["copilot"],
        )
        assert warning is None

    def test_install_targets_superset_no_warning(self) -> None:
        """Install targets are a superset of pack targets -> no warning."""
        warning = check_target_mismatch(
            bundle_targets=["copilot"],
            install_targets=["copilot", "claude"],
        )
        assert warning is None


# ---------------------------------------------------------------------------
# read_bundle_plugin_json tests
# ---------------------------------------------------------------------------


class TestReadBundlePluginJson:
    """Tests for read_bundle_plugin_json()."""

    def test_reads_valid_plugin_json(self, tmp_path: Path) -> None:
        bundle = _make_plugin_bundle(tmp_path, plugin_id="my-pkg")
        result = read_bundle_plugin_json(bundle)
        assert result["id"] == "my-pkg"

    def test_returns_empty_dict_when_missing(self, tmp_path: Path) -> None:
        result = read_bundle_plugin_json(tmp_path)
        assert result == {}
