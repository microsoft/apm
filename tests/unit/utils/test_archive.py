"""Safety tests for archive extraction helpers."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from apm_cli.utils import archive as archive_mod
from apm_cli.utils.archive import (
    ArchiveError,
    _detect_archive_format,
    _extract_tar_gz,
    _extract_zip,
    download_and_extract_archive,
)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_gz_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_detect_archive_format_rejects_uncompressed_tar() -> None:
    with pytest.raises(ArchiveError, match=r"gzip-compressed tarballs"):
        _detect_archive_format("application/x-tar", "https://example.test/archive.tar")


def test_download_and_extract_archive_rejects_non_https_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_get(_url: str, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("network should not be called for non-HTTPS archives")

    monkeypatch.setattr(archive_mod.requests, "get", fake_get)

    with pytest.raises(ArchiveError, match="Only HTTPS URLs"):
        download_and_extract_archive("http://example.test/plugin.zip", str(tmp_path / "out"))

    assert called is False


def test_download_and_extract_archive_rejects_redirect_to_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_bytes({"plugin/SKILL.md": b"content"})

    class Response:
        def __init__(self) -> None:
            self.headers = {"Content-Type": ""}
            self.url = "http://cdn.example.test/plugin.zip"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            yield archive

        def close(self) -> None:
            return None

    def fake_get(_url: str, **_kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr(archive_mod.requests, "get", fake_get)

    with pytest.raises(ArchiveError, match="Redirect to non-HTTPS URL rejected"):
        download_and_extract_archive("https://example.test/download", str(tmp_path / "out"))


def test_download_and_extract_archive_stages_raw_file_outside_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_bytes({"plugin/SKILL.md": b"content"})
    out = tmp_path / "out"

    class Response:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "application/zip"}
            self.url = "https://cdn.example.test/plugin.zip"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            assert not list(out.glob(".apm-archive-download-*"))
            yield archive

        def close(self) -> None:
            return None

    def fake_get(_url: str, **_kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr(archive_mod.requests, "get", fake_get)

    extracted = download_and_extract_archive("https://example.test/download", str(out))

    assert extracted == ["plugin/SKILL.md"]
    assert not list(out.glob(".apm-archive-*"))


def test_download_and_extract_archive_uses_redirect_url_for_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip_bytes({"plugin/SKILL.md": b"content"})
    seen_kwargs: list[dict[str, object]] = []

    class Response:
        def __init__(self) -> None:
            self.headers = {"Content-Type": ""}
            self.url = "https://cdn.example.test/plugin.zip"

        @property
        def content(self) -> bytes:
            raise AssertionError(
                "download_and_extract_archive must stream, not read response.content"
            )

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            for idx in range(0, len(archive), chunk_size):
                yield archive[idx : idx + chunk_size]

        def close(self) -> None:
            return None

    def fake_get(_url: str, **kwargs: object) -> Response:
        seen_kwargs.append(kwargs)
        return Response()

    monkeypatch.setattr(archive_mod.requests, "get", fake_get)

    out = tmp_path / "out"
    extracted = download_and_extract_archive("https://example.test/download", str(out))

    assert seen_kwargs == [{"headers": {"User-Agent": "apm-cli"}, "timeout": 60, "stream": True}]
    assert extracted == ["plugin/SKILL.md"]
    assert (out / "plugin" / "SKILL.md").read_text() == "content"


def test_extract_zip_rejects_path_traversal(tmp_path: Path) -> None:
    archive = _zip_bytes({"../escape.txt": b"nope"})

    with pytest.raises(ArchiveError, match=r"traversal|outside"):
        _extract_zip(archive, str(tmp_path / "out"))


def test_extract_tar_gz_rejects_symlink(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "target"
        tf.addfile(info)

    with pytest.raises(ArchiveError, match="links are not supported"):
        _extract_tar_gz(buf.getvalue(), str(tmp_path / "out"))


def test_extract_zip_rejects_decompression_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_mod, "_MAX_UNCOMPRESSED_BYTES", 1024)
    archive = _zip_bytes({"plugin/big.bin": b"x" * 4096})

    with pytest.raises(ArchiveError, match=r"exceeds size limit"):
        _extract_zip(archive, str(tmp_path / "out"))


def test_extract_tar_gz_rejects_decompression_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_mod, "_MAX_UNCOMPRESSED_BYTES", 1024)
    archive = _tar_gz_bytes({"plugin/big.bin": b"x" * 4096})

    with pytest.raises(ArchiveError, match=r"exceeds size limit"):
        _extract_tar_gz(archive, str(tmp_path / "out"))


def test_extract_zip_writes_safe_members(tmp_path: Path) -> None:
    archive = _zip_bytes({"plugin/SKILL.md": b"content"})
    out = tmp_path / "out"

    extracted = _extract_zip(archive, str(out))

    assert extracted == ["plugin/SKILL.md"]
    assert (out / "plugin" / "SKILL.md").read_text() == "content"


def test_extract_tar_gz_writes_safe_members(tmp_path: Path) -> None:
    archive = _tar_gz_bytes({"plugin/SKILL.md": b"content"})
    out = tmp_path / "out"

    extracted = _extract_tar_gz(archive, str(out))

    assert extracted == ["plugin/SKILL.md"]
    assert (out / "plugin" / "SKILL.md").read_text() == "content"
