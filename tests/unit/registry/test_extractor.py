"""Tests for tarball extraction with sha256 verification.

The extractor is the security-critical gate on the install path (design §6.1):
hash mismatches and traversal attempts MUST fail closed.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from apm_cli.deps.registry.extractor import (
    HashMismatchError,
    UnknownArchiveFormatError,
    UnsafeTarballError,
    _normalize_digest,
    extract_archive,
    extract_tarball,
    extract_zip,
    verify_sha256,
)


def _build_tar(entries: list[tuple[str, bytes]], *, mode: str = "w:gz") -> bytes:
    """Build an in-memory tarball from a list of (name, content) pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, content in entries:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def _build_tar_with(extra_setup) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        extra_setup(tar)
    return buf.getvalue()


class TestNormalizeDigest:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("abc", "abc"),
            ("ABC", "abc"),
            ("sha256:abc", "abc"),
            ("sha256=ABC", "abc"),
            ("  sha256:abc  ", "abc"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _normalize_digest(raw) == expected


class TestVerifySha256:
    def test_match_returns_actual_hex(self):
        data = b"hello"
        h = hashlib.sha256(data).hexdigest()
        assert verify_sha256(data, h) == h

    def test_match_with_prefix(self):
        data = b"hello"
        h = hashlib.sha256(data).hexdigest()
        assert verify_sha256(data, f"sha256:{h}") == h

    def test_mismatch_raises(self):
        with pytest.raises(HashMismatchError):
            verify_sha256(b"hello", "0" * 64)


class TestExtractTarball:
    def test_extract_validates_hash_first(self, tmp_path):
        data = _build_tar([("apm.yml", b"name: x\n")])
        with pytest.raises(HashMismatchError):
            extract_tarball(data, "0" * 64, tmp_path)
        # Nothing extracted on hash failure
        assert not list(tmp_path.iterdir())

    def test_extract_writes_files(self, tmp_path):
        data = _build_tar(
            [("apm.yml", b"name: acme/x\n"), (".apm/.keep", b"")]
        )
        digest = hashlib.sha256(data).hexdigest()
        actual = extract_tarball(data, digest, tmp_path)
        assert actual == digest
        assert (tmp_path / "apm.yml").read_bytes() == b"name: acme/x\n"
        assert (tmp_path / ".apm" / ".keep").exists()

    def test_extract_accepts_sha256_prefix(self, tmp_path):
        data = _build_tar([("apm.yml", b"name: y\n")])
        digest = hashlib.sha256(data).hexdigest()
        extract_tarball(data, f"sha256:{digest}", tmp_path)
        assert (tmp_path / "apm.yml").exists()

    # ─── Path traversal & unsafe entries ────────────────────────────────

    def test_rejects_absolute_path(self, tmp_path):
        data = _build_tar([("/etc/passwd", b"x")])
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError):
            extract_tarball(data, digest, tmp_path)

    def test_rejects_path_traversal(self, tmp_path):
        data = _build_tar([("../escape.txt", b"x")])
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError):
            extract_tarball(data, digest, tmp_path)

    def test_rejects_symlink(self, tmp_path):
        def add_symlink(tar):
            ti = tarfile.TarInfo(name="link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)

        data = _build_tar_with(add_symlink)
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError):
            extract_tarball(data, digest, tmp_path)

    def test_rejects_hard_link(self, tmp_path):
        def add_hardlink(tar):
            ti = tarfile.TarInfo(name="link")
            ti.type = tarfile.LNKTYPE
            ti.linkname = "apm.yml"
            tar.addfile(ti)

        data = _build_tar_with(add_hardlink)
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError):
            extract_tarball(data, digest, tmp_path)

    def test_creates_intermediate_directories(self, tmp_path):
        data = _build_tar([("a/b/c/file.txt", b"hello")])
        digest = hashlib.sha256(data).hexdigest()
        extract_tarball(data, digest, tmp_path)
        assert (tmp_path / "a" / "b" / "c" / "file.txt").read_bytes() == b"hello"


def _build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Build an in-memory zip from (name, content) pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


class TestExtractZip:
    """Same security gates as the tar path, applied to zip archives."""

    def test_validates_hash_first(self, tmp_path):
        data = _build_zip([("apm.yml", b"name: x\n")])
        with pytest.raises(HashMismatchError):
            extract_zip(data, "0" * 64, tmp_path)
        assert not list(tmp_path.iterdir())

    def test_writes_files(self, tmp_path):
        data = _build_zip([("apm.yml", b"name: y\n"), (".apm/.keep", b"")])
        digest = hashlib.sha256(data).hexdigest()
        extract_zip(data, digest, tmp_path)
        assert (tmp_path / "apm.yml").read_bytes() == b"name: y\n"
        assert (tmp_path / ".apm" / ".keep").exists()

    def test_rejects_path_traversal(self, tmp_path):
        data = _build_zip([("../escape.txt", b"x")])
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError):
            extract_zip(data, digest, tmp_path)

    def test_rejects_absolute_path(self, tmp_path):
        data = _build_zip([("/etc/passwd", b"x")])
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError):
            extract_zip(data, digest, tmp_path)

    def test_rejects_symlink(self, tmp_path):
        # Build a zip with a symlink entry by setting external_attr to S_IFLNK.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w") as zf:
            info = zipfile.ZipInfo("link")
            # 0xA000 == S_IFLNK; place in high 16 bits
            info.external_attr = (0xA000 | 0o777) << 16
            zf.writestr(info, b"/etc/passwd")
        data = buf.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsafeTarballError, match="symlink"):
            extract_zip(data, digest, tmp_path)

    def test_malformed_zip(self, tmp_path):
        # Hash check happens first, so we have to feed the right digest for
        # the bytes we're sending — the BadZipFile path is what we want to test.
        bad = b"not a zip"
        digest = hashlib.sha256(bad).hexdigest()
        with pytest.raises(UnknownArchiveFormatError):
            extract_zip(bad, digest, tmp_path)


class TestExtractArchiveDispatcher:
    """The dispatcher picks the right extractor from Content-Type or magic bytes."""

    def test_dispatches_to_tar_for_gzip_content_type(self, tmp_path):
        data = _build_tar([("apm.yml", b"name: x\n")])
        digest = hashlib.sha256(data).hexdigest()
        extract_archive(data, digest, tmp_path, content_type="application/gzip")
        assert (tmp_path / "apm.yml").exists()

    def test_dispatches_to_zip_for_zip_content_type(self, tmp_path):
        data = _build_zip([("apm.yml", b"name: x\n")])
        digest = hashlib.sha256(data).hexdigest()
        extract_archive(data, digest, tmp_path, content_type="application/zip")
        assert (tmp_path / "apm.yml").exists()

    def test_falls_back_to_magic_bytes_for_gzip(self, tmp_path):
        data = _build_tar([("apm.yml", b"name: x\n")])
        digest = hashlib.sha256(data).hexdigest()
        extract_archive(data, digest, tmp_path, content_type=None)
        assert (tmp_path / "apm.yml").exists()

    def test_falls_back_to_magic_bytes_for_zip(self, tmp_path):
        data = _build_zip([("apm.yml", b"name: x\n")])
        digest = hashlib.sha256(data).hexdigest()
        extract_archive(data, digest, tmp_path, content_type=None)
        assert (tmp_path / "apm.yml").exists()

    def test_unknown_format_raises(self, tmp_path):
        # Random bytes with no recognizable magic header.
        data = b"not-an-archive-of-any-known-type"
        digest = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnknownArchiveFormatError):
            extract_archive(data, digest, tmp_path)

    def test_hash_mismatch_fails_before_extraction_for_both(self, tmp_path):
        for data in (
            _build_tar([("apm.yml", b"x")]),
            _build_zip([("apm.yml", b"x")]),
        ):
            with pytest.raises(HashMismatchError):
                extract_archive(data, "0" * 64, tmp_path)
            assert not list(tmp_path.iterdir())

    def test_unrecognized_content_type_falls_back_to_magic(self, tmp_path):
        # Server sends a generic content type — magic bytes should still
        # win and identify the archive.
        data = _build_tar([("apm.yml", b"x")])
        digest = hashlib.sha256(data).hexdigest()
        extract_archive(
            data, digest, tmp_path, content_type="application/octet-stream"
        )
        assert (tmp_path / "apm.yml").exists()
