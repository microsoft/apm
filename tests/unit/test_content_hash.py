"""Tests for SHA-256 content integrity hashing."""

import os  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from apm_cli.utils.content_hash import compute_package_hash, verify_package_hash

# ---------------------------------------------------------------------------
# compute_package_hash
# ---------------------------------------------------------------------------


class TestComputePackageHash:
    def test_basic_hash(self, tmp_path):
        """Computes deterministic hash for a package directory."""
        (tmp_path / "file.txt").write_text("hello")
        result = compute_package_hash(tmp_path)
        assert result.startswith("sha256:")
        assert len(result) == len("sha256:") + 64  # SHA-256 hex digest is 64 chars

    def test_deterministic_across_calls(self, tmp_path):
        """Same content produces same hash."""
        (tmp_path / "a.txt").write_text("content")
        assert compute_package_hash(tmp_path) == compute_package_hash(tmp_path)

    def test_different_content_different_hash(self, tmp_path):
        """Different file content produces different hash."""
        (tmp_path / "a.txt").write_text("version1")
        hash1 = compute_package_hash(tmp_path)
        (tmp_path / "a.txt").write_text("version2")
        hash2 = compute_package_hash(tmp_path)
        assert hash1 != hash2

    def test_file_order_independent(self, tmp_path):
        """Hash is the same regardless of filesystem ordering."""
        # Create files in two different orders, hash should be the same
        d1 = tmp_path / "dir1"
        d1.mkdir()
        (d1 / "b.txt").write_text("B")
        (d1 / "a.txt").write_text("A")

        d2 = tmp_path / "dir2"
        d2.mkdir()
        (d2 / "a.txt").write_text("A")
        (d2 / "b.txt").write_text("B")

        assert compute_package_hash(d1) == compute_package_hash(d2)

    def test_skips_git_directory(self, tmp_path):
        """The .git directory is excluded from hashing."""
        (tmp_path / "code.py").write_text("print('hi')")
        hash_before = compute_package_hash(tmp_path)

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main")
        hash_after = compute_package_hash(tmp_path)

        assert hash_before == hash_after

    def test_skips_pycache(self, tmp_path):
        """__pycache__ directories are excluded from hashing."""
        (tmp_path / "module.py").write_text("x = 1")
        hash_before = compute_package_hash(tmp_path)

        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "module.cpython-312.pyc").write_bytes(b"\x00\x01\x02")
        hash_after = compute_package_hash(tmp_path)

        assert hash_before == hash_after

    def test_empty_directory(self, tmp_path):
        """Empty directory returns a well-known hash."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = compute_package_hash(empty)
        assert result.startswith("sha256:")
        # Empty hash is the SHA-256 of an empty bytestring
        import hashlib

        expected = "sha256:" + hashlib.sha256(b"").hexdigest()
        assert result == expected

    def test_nonexistent_directory(self, tmp_path):
        """Non-existent path returns the empty hash."""
        import hashlib

        expected = "sha256:" + hashlib.sha256(b"").hexdigest()
        assert compute_package_hash(tmp_path / "nope") == expected

    def test_binary_files_handled(self, tmp_path):
        """Binary files are hashed correctly."""
        (tmp_path / "data.bin").write_bytes(bytes(range(256)))
        result = compute_package_hash(tmp_path)
        assert result.startswith("sha256:")
        # Verify it doesn't raise and produces a valid digest
        assert len(result) == len("sha256:") + 64

    def test_symlinks_skipped(self, tmp_path):
        """Symlinks are not followed during hashing."""
        (tmp_path / "real.txt").write_text("real")
        hash_before = compute_package_hash(tmp_path)

        # Create a symlink
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(tmp_path / "real.txt")
        except OSError:
            pytest.skip("Cannot create symlinks on this platform")
        hash_after = compute_package_hash(tmp_path)
        assert hash_before == hash_after

    def test_hash_format(self, tmp_path):
        """Hash starts with 'sha256:' prefix."""
        (tmp_path / "f.txt").write_text("x")
        result = compute_package_hash(tmp_path)
        assert result.startswith("sha256:")
        hex_part = result[len("sha256:") :]
        # Validate it's a valid hex string
        int(hex_part, 16)

    def test_nested_directories(self, tmp_path):
        """Nested directory structure is hashed correctly."""
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "nested.txt").write_text("deep content")
        (tmp_path / "top.txt").write_text("top content")
        result = compute_package_hash(tmp_path)
        assert result.startswith("sha256:")

    def test_path_uses_posix_format(self, tmp_path):
        """File paths use POSIX separators for cross-platform determinism."""
        sub = tmp_path / "dir"
        sub.mkdir()
        (sub / "file.txt").write_text("content")
        # Hash should be the same on any platform (POSIX paths used internally)
        hash1 = compute_package_hash(tmp_path)
        hash2 = compute_package_hash(tmp_path)
        assert hash1 == hash2


# ---------------------------------------------------------------------------
# verify_package_hash
# ---------------------------------------------------------------------------


class TestVerifyPackageHash:
    def test_matching_hash(self, tmp_path):
        """Verification passes when content matches."""
        (tmp_path / "a.txt").write_text("hello")
        expected = compute_package_hash(tmp_path)
        assert verify_package_hash(tmp_path, expected) is True

    def test_mismatched_hash(self, tmp_path):
        """Verification fails when content changed."""
        (tmp_path / "a.txt").write_text("original")
        expected = compute_package_hash(tmp_path)
        (tmp_path / "a.txt").write_text("tampered")
        assert verify_package_hash(tmp_path, expected) is False

    def test_missing_file_fails(self, tmp_path):
        """Verification fails when file is deleted."""
        (tmp_path / "a.txt").write_text("data")
        (tmp_path / "b.txt").write_text("more")
        expected = compute_package_hash(tmp_path)
        (tmp_path / "b.txt").unlink()
        assert verify_package_hash(tmp_path, expected) is False

    def test_added_file_fails(self, tmp_path):
        """Verification fails when an extra file is added."""
        (tmp_path / "a.txt").write_text("data")
        expected = compute_package_hash(tmp_path)
        (tmp_path / "extra.txt").write_text("injected")
        assert verify_package_hash(tmp_path, expected) is False


# ---------------------------------------------------------------------------
# Lockfile integration
# ---------------------------------------------------------------------------


class TestLockfileContentHash:
    def test_content_hash_serialized(self):
        """content_hash appears in lockfile YAML output."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency(
            repo_url="owner/repo",
            content_hash="sha256:abc123",
        )
        d = dep.to_dict()
        assert d["content_hash"] == "sha256:abc123"

    def test_content_hash_deserialized(self):
        """content_hash is read back from lockfile."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency.from_dict(
            {
                "repo_url": "owner/repo",
                "content_hash": "sha256:abc123",
            }
        )
        assert dep.content_hash == "sha256:abc123"

    def test_missing_content_hash_backward_compat(self):
        """Old lockfiles without content_hash parse fine (None)."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency.from_dict(
            {
                "repo_url": "owner/repo",
            }
        )
        assert dep.content_hash is None

    def test_content_hash_none_not_emitted(self):
        """content_hash=None is not written to YAML."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency(
            repo_url="owner/repo",
            content_hash=None,
        )
        d = dep.to_dict()
        assert "content_hash" not in d

    def test_content_hash_roundtrip_yaml(self, tmp_path):
        """content_hash survives a full write/read YAML cycle."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        lockfile = LockFile(apm_version="test")
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            content_hash="sha256:deadbeef",
        )
        lockfile.add_dependency(dep)
        path = tmp_path / "apm.lock.yaml"
        lockfile.save(path)

        loaded = LockFile.read(path)
        assert loaded is not None
        loaded_dep = loaded.get_dependency("owner/repo")
        assert loaded_dep is not None
        assert loaded_dep.content_hash == "sha256:deadbeef"
