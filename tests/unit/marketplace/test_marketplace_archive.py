"""Tests for archive download and extraction (Step 7 -- gap #14).

Covers: _check_archive_member safety checks, _detect_archive_format, _extract_tar_gz,
_extract_zip, and the download_and_extract_archive public API.
"""

import io
import os
import tarfile
import zipfile

import pytest

from apm_cli.marketplace.archive import (
    ArchiveError,
    _check_archive_member,
    _detect_archive_format,
    _extract_tar_gz,
    _extract_zip,
    download_and_extract_archive,
)
from apm_cli.marketplace.errors import MarketplaceFetchError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tar_gz(members: dict) -> bytes:
    """Build an in-memory .tar.gz from {path: content_bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_zip(members: dict) -> bytes:
    """Build an in-memory .zip from {path: content_bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Step 7 tests -- safety checks
# ---------------------------------------------------------------------------


class TestArchiveMemberSafetyChecks:
    """_check_archive_member must reject dangerous paths."""

    @pytest.mark.parametrize("path", [
        "skills/my-skill.md",
        "README.md",
        "a/b/c/d.txt",
        "subdir/file-with-time-12:00.txt",
    ])
    def test_safe_paths_accepted(self, path):
        _check_archive_member(path)

    @pytest.mark.parametrize("path", [
        "../etc/passwd",
        "skills/../../etc/passwd",
    ])
    def test_path_traversal_rejected(self, path):
        with pytest.raises(ArchiveError, match="traversal"):
            _check_archive_member(path)

    def test_absolute_path_rejected(self):
        with pytest.raises(ArchiveError, match="absolute"):
            _check_archive_member("/etc/passwd")

    def test_null_byte_in_path_rejected(self):
        with pytest.raises(ArchiveError):
            _check_archive_member("skills/skill\x00.md")

    @pytest.mark.parametrize("path", [
        "C:\\windows\\system32\\evil.exe",
        "C:/windows/system32/evil.exe",
        "D:\\",
        "\\\\server\\share\\file.txt",
    ])
    def test_windows_absolute_paths_rejected(self, path):
        with pytest.raises(ArchiveError, match="absolute"):
            _check_archive_member(path)


# ---------------------------------------------------------------------------
# Step 7 tests -- format detection
# ---------------------------------------------------------------------------


class TestDetectArchiveFormat:
    """_detect_archive_format must identify tar.gz and zip from Content-Type or URL."""

    @pytest.mark.parametrize("content_type,expected", [
        ("application/gzip", "tar.gz"),
        ("application/x-gzip", "tar.gz"),
        ("application/x-tar", "tar.gz"),
        ("application/zip", "zip"),
        ("application/x-zip-compressed", "zip"),
        ("application/gzip; charset=utf-8", "tar.gz"),
    ])
    def test_detect_archive_format_from_content_type(self, content_type, expected):
        assert _detect_archive_format(content_type, "") == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://example.com/skill.tar.gz", "tar.gz"),
        ("https://example.com/skill.tgz", "tar.gz"),
        ("https://example.com/skill.zip", "zip"),
    ])
    def test_detect_archive_format_from_url_extension(self, url, expected):
        assert _detect_archive_format("", url) == expected

    def test_content_type_takes_priority_over_url(self):
        assert _detect_archive_format("application/zip", "https://example.com/skill.tar.gz") == "zip"

    def test_unknown_format_raises_archive_error(self):
        with pytest.raises(ArchiveError, match="format"):
            _detect_archive_format("text/html", "https://example.com/skill.html")


# ---------------------------------------------------------------------------
# Step 7 tests -- tar.gz extraction
# ---------------------------------------------------------------------------


class TestExtractTarGz:
    """_extract_tar_gz must extract files and enforce safety checks."""

    @pytest.mark.parametrize("path,content", [
        ("skill.md", b"# My Skill"),
        ("skills/my-skill.md", b"content"),
    ])
    def test_extract_file(self, tmp_path, path, content):
        data = _make_tar_gz({path: content})
        paths = _extract_tar_gz(data, str(tmp_path))
        assert path in paths
        assert (tmp_path / path).read_bytes() == content

    def test_path_traversal_member_rejected(self, tmp_path):
        data = _make_tar_gz({"../evil.txt": b"bad"})
        with pytest.raises(ArchiveError, match="traversal"):
            _extract_tar_gz(data, str(tmp_path))

    def test_decompression_bomb_rejected(self, tmp_path):
        large_content = b"x" * (600 * 1024 * 1024)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="bomb.txt")
            info.size = len(large_content)
            tf.addfile(info, io.BytesIO(large_content))
        with pytest.raises(ArchiveError, match="size limit|bomb"):
            _extract_tar_gz(buf.getvalue(), str(tmp_path))

    def test_multiple_files_returned(self, tmp_path):
        data = _make_tar_gz({"a.md": b"a", "b.md": b"b"})
        paths = _extract_tar_gz(data, str(tmp_path))
        assert sorted(paths) == ["a.md", "b.md"]

    def test_directory_entries_skipped(self, tmp_path):
        """Directory members in tar must be skipped without error."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            d = tarfile.TarInfo(name="subdir/")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
            f = tarfile.TarInfo(name="subdir/file.txt")
            f.size = 5
            tf.addfile(f, io.BytesIO(b"hello"))
        paths = _extract_tar_gz(buf.getvalue(), str(tmp_path))
        assert paths == ["subdir/file.txt"]

    def test_empty_archive_returns_empty_list(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            pass
        assert _extract_tar_gz(buf.getvalue(), str(tmp_path)) == []


# ---------------------------------------------------------------------------
# Step 7 tests -- zip extraction
# ---------------------------------------------------------------------------


class TestExtractZip:
    """_extract_zip must extract files and enforce safety checks."""

    @pytest.mark.parametrize("path,content", [
        ("skill.md", b"# My Skill"),
        ("skills/my-skill.md", b"content"),
    ])
    def test_extract_file(self, tmp_path, path, content):
        data = _make_zip({path: content})
        paths = _extract_zip(data, str(tmp_path))
        assert path in paths
        assert (tmp_path / path).read_bytes() == content

    def test_path_traversal_member_rejected(self, tmp_path):
        data = _make_zip({"../evil.txt": b"bad"})
        with pytest.raises(ArchiveError, match="traversal"):
            _extract_zip(data, str(tmp_path))

    def test_absolute_path_member_rejected(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/passwd", "root:x:0:0")
        with pytest.raises(ArchiveError, match="absolute"):
            _extract_zip(buf.getvalue(), str(tmp_path))

    def test_decompression_bomb_rejected(self, tmp_path):
        large = b"x" * (600 * 1024 * 1024)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bomb.txt", large)
        with pytest.raises(ArchiveError, match="size limit|bomb"):
            _extract_zip(buf.getvalue(), str(tmp_path))

    def test_corrupted_zip_raises_archive_error(self, tmp_path):
        with pytest.raises(ArchiveError, match="zip"):
            _extract_zip(b"not a zip file", str(tmp_path))


# ---------------------------------------------------------------------------
# Step 7 tests -- download_and_extract_archive
# ---------------------------------------------------------------------------


class TestDownloadAndExtractArchive:
    """download_and_extract_archive must download, detect format, and extract."""

    def test_successful_download_and_extract_tar_gz(self, tmp_path, monkeypatch):
        import unittest.mock as mock

        archive_bytes = _make_tar_gz({"skill.md": b"# Hello"})
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/gzip"}
        resp.content = archive_bytes
        resp.raise_for_status.return_value = None
        monkeypatch.setattr("apm_cli.marketplace.archive.requests.get",
                            lambda *a, **kw: resp)

        paths = download_and_extract_archive(
            "https://example.com/skill.tar.gz", str(tmp_path)
        )
        assert "skill.md" in paths
        assert (tmp_path / "skill.md").read_bytes() == b"# Hello"

    def test_successful_download_and_extract_zip(self, tmp_path, monkeypatch):
        import unittest.mock as mock

        archive_bytes = _make_zip({"skill.md": b"# Hello"})
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/zip"}
        resp.content = archive_bytes
        resp.raise_for_status.return_value = None
        monkeypatch.setattr("apm_cli.marketplace.archive.requests.get",
                            lambda *a, **kw: resp)

        paths = download_and_extract_archive(
            "https://example.com/skill.zip", str(tmp_path)
        )
        assert "skill.md" in paths

    def test_404_raises_archive_error(self, tmp_path, monkeypatch):
        import unittest.mock as mock
        import requests as req

        resp = mock.MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = req.exceptions.HTTPError("404")
        monkeypatch.setattr("apm_cli.marketplace.archive.requests.get",
                            lambda *a, **kw: resp)

        with pytest.raises(ArchiveError, match="404|download"):
            download_and_extract_archive(
                "https://example.com/missing.tar.gz", str(tmp_path)
            )

    def test_unknown_content_type_raises_archive_error(self, tmp_path, monkeypatch):
        import unittest.mock as mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/html"}
        resp.content = b"<html>not an archive</html>"
        resp.raise_for_status.return_value = None
        monkeypatch.setattr("apm_cli.marketplace.archive.requests.get",
                            lambda *a, **kw: resp)

        with pytest.raises(ArchiveError, match="format"):
            download_and_extract_archive(
                "https://example.com/index.html", str(tmp_path)
            )


# ---------------------------------------------------------------------------
# Symlink and hard link safety in tar.gz
# ---------------------------------------------------------------------------


class TestSymlinkAndHardlinkSafety:
    """tar.gz archives containing symlinks or hard links must be rejected."""

    def _make_symlink_tar_gz(self, link_name: str, link_target: str) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name=link_name)
            info.type = tarfile.SYMTYPE
            info.linkname = link_target
            tf.addfile(info)
        return buf.getvalue()

    def _make_hardlink_tar_gz(
        self, real_name: str, link_name: str, link_target: str
    ) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            real = tarfile.TarInfo(name=real_name)
            real.size = 5
            tf.addfile(real, io.BytesIO(b"hello"))
            link = tarfile.TarInfo(name=link_name)
            link.type = tarfile.LNKTYPE
            link.linkname = link_target
            tf.addfile(link)
        return buf.getvalue()

    def test_symlink_raises_archive_error(self, tmp_path):
        data = self._make_symlink_tar_gz("link.txt", "../etc/passwd")
        with pytest.raises(ArchiveError, match="[Ss]ymlink|[Ll]ink"):
            _extract_tar_gz(data, str(tmp_path))

    def test_hardlink_raises_archive_error(self, tmp_path):
        data = self._make_hardlink_tar_gz("real.txt", "link.txt", "../evil.txt")
        with pytest.raises(ArchiveError, match="[Ss]ymlink|[Ll]ink|[Hh]ard"):
            _extract_tar_gz(data, str(tmp_path))

    def test_symlink_error_is_archive_error_not_key_error(self, tmp_path):
        """KeyError from extractfile must be wrapped in ArchiveError."""
        data = self._make_symlink_tar_gz("link.txt", "real.txt")
        with pytest.raises(ArchiveError):
            _extract_tar_gz(data, str(tmp_path))


# ---------------------------------------------------------------------------
# Non-regular tar members (device files, FIFOs) -- C09
# ---------------------------------------------------------------------------


class TestNonRegularTarMembers:
    """Non-regular tar members (device files, FIFOs) must be skipped."""

    def _make_tar_with_member_type(self, name: str, member_type: int) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name=name)
            info.type = member_type
            tf.addfile(info)
        return buf.getvalue()

    def test_device_file_skipped(self, tmp_path):
        data = self._make_tar_with_member_type("dev/sda", tarfile.CHRTYPE)
        extracted = _extract_tar_gz(data, str(tmp_path))
        assert extracted == []

    def test_fifo_skipped(self, tmp_path):
        data = self._make_tar_with_member_type("pipe", tarfile.FIFOTYPE)
        extracted = _extract_tar_gz(data, str(tmp_path))
        assert extracted == []

    def test_block_device_skipped(self, tmp_path):
        data = self._make_tar_with_member_type("dev/blk", tarfile.BLKTYPE)
        extracted = _extract_tar_gz(data, str(tmp_path))
        assert extracted == []

    def test_regular_file_still_extracted(self, tmp_path):
        data = _make_tar_gz({"hello.txt": b"world"})
        extracted = _extract_tar_gz(data, str(tmp_path))
        assert "hello.txt" in extracted
        assert (tmp_path / "hello.txt").read_bytes() == b"world"


# ---------------------------------------------------------------------------
# Archive HTTPS enforcement -- C08
# ---------------------------------------------------------------------------


class TestArchiveHttpsEnforcement:
    """download_and_extract_archive must enforce HTTPS."""

    def test_http_url_raises(self, tmp_path):
        with pytest.raises(ArchiveError, match="HTTPS"):
            download_and_extract_archive("http://example.com/a.tar.gz", str(tmp_path))

    def test_ftp_url_raises(self, tmp_path):
        with pytest.raises(ArchiveError, match="HTTPS"):
            download_and_extract_archive("ftp://example.com/a.tar.gz", str(tmp_path))

    def test_redirect_to_http_raises(self, tmp_path, monkeypatch):
        import unittest.mock as mock

        resp = mock.MagicMock()
        resp.status_code = 200
        resp.url = "http://evil.com/a.tar.gz"
        resp.headers = {"Content-Type": "application/gzip"}
        resp.content = b""
        resp.raise_for_status.return_value = None
        monkeypatch.setattr("apm_cli.marketplace.archive.requests.get",
                            lambda *a, **kw: resp)

        with pytest.raises(ArchiveError, match="non-HTTPS"):
            download_and_extract_archive("https://example.com/a.tar.gz", str(tmp_path))
