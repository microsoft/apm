"""Safety tests for archive extraction helpers."""

from __future__ import annotations

import io
import os
import sys
import tarfile
import threading
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
    write_zip_archive,
)

pytestmark = pytest.mark.component


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

    monkeypatch.setattr(archive_mod, "_archive_get", fake_get)

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

    monkeypatch.setattr(archive_mod, "_archive_get", fake_get)

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

    monkeypatch.setattr(archive_mod, "_archive_get", fake_get)

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

    monkeypatch.setattr(archive_mod, "_archive_get", fake_get)

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


def test_write_zip_archive_excludes_symlinks(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "plugin.json").write_text("{}", encoding="utf-8")
    (bundle_dir / "link").symlink_to("plugin.json")
    archive_path = tmp_path / "bundle.zip"

    write_zip_archive(bundle_dir, archive_path)

    assert archive_path.is_file()
    with zipfile.ZipFile(archive_path, "r") as zf:
        assert zf.namelist() == ["bundle/plugin.json"]


def test_extract_tar_gz_writes_safe_members(tmp_path: Path) -> None:
    archive = _tar_gz_bytes({"plugin/SKILL.md": b"content"})
    out = tmp_path / "out"

    extracted = _extract_tar_gz(archive, str(out))

    assert extracted == ["plugin/SKILL.md"]
    assert (out / "plugin" / "SKILL.md").read_text() == "content"


# -- Entry names a filesystem would rewrite or reinterpret ----------------------
# Refused on every OS: Windows drops a trailing dot or space, treats ':' as an
# alternate data stream and maps reserved device names, so such a name would
# not land where it says.

_UNPORTABLE_NAMES = [
    ".. /x.md",
    "a/.. /x.md",
    ".../x.md",
    "a/.../x.md",
    "a./x.md",
    "a /x.md",
    "x.md.",
    "x.md ",
    "a.md:x",
    "a.md::$DATA",
    "CON",
    "con.md",
    "aux.md",
    "NUL",
    "nul.txt",
    "COM1.txt",
    "Com1.md",
    "LPT9",
    "a/PRN.md",
    "COM0.md",
    "lpt0",
    "COM\u00b9.md",
    "LPT\u00b3",
    "CONIN$",
    "conout$.txt",
    "a\x01b.md",
    "a/\x1f.md",
]
_PORTABLE_NAMES = ["a.b.md", "v1.2/notes.md", "console.md", "COM10.md", "con_notes/x.md"]
_FORMATS = ["tar.gz", "zip"]


def _archive_entries(fmt: str, entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries:
                zf.writestr(zipfile.ZipInfo(name), data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in entries:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _extract_watched(fmt: str, entries: list[tuple[str, bytes]], dest: Path) -> dict:
    """Extract in a daemon thread; ``"error"`` is missing if it did not finish in 20 s."""
    data = _archive_entries(fmt, entries)
    extract = _extract_zip if fmt == "zip" else _extract_tar_gz
    outcome: dict = {}

    def run() -> None:
        try:
            extract(data, str(dest))
            outcome["error"] = None
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=20)
    return outcome


@pytest.mark.parametrize("name", _UNPORTABLE_NAMES)
@pytest.mark.parametrize("fmt", _FORMATS)
def test_extract_refuses_an_unportable_entry_name(tmp_path: Path, fmt: str, name: str) -> None:
    dest = tmp_path / "dest"

    outcome = _extract_watched(fmt, [("ok.md", b"ok"), (name, b"x")], dest)

    assert "error" in outcome, "extraction did not finish"
    assert isinstance(outcome["error"], ArchiveError), outcome["error"]
    assert not dest.exists() or not any(dest.rglob("*"))
    assert set(dest.parent.iterdir()) <= {dest}


@pytest.mark.parametrize("name", _PORTABLE_NAMES)
@pytest.mark.parametrize("fmt", _FORMATS)
def test_extract_keeps_an_ordinary_dotted_name(tmp_path: Path, fmt: str, name: str) -> None:
    dest = tmp_path / "dest"

    outcome = _extract_watched(fmt, [(name, b"x")], dest)

    assert outcome == {"error": None}
    assert (dest / name).read_bytes() == b"x"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows name trimming")
@pytest.mark.parametrize("name", [".. /x.md", "a/.. /.. /x.md", "a/.../x.md", "a. /x.md"])
@pytest.mark.parametrize("fmt", _FORMATS)
def test_trimmed_names_never_land_outside_dest_on_windows(
    tmp_path: Path, fmt: str, name: str
) -> None:
    dest = tmp_path / "outer" / "dest"

    outcome = _extract_watched(fmt, [("ok.md", b"ok"), (name, b"x")], dest)

    outside = [p for p in tmp_path.rglob("x.md") if dest not in p.parents]
    assert outside == []
    assert "error" in outcome, "extraction did not finish"
    assert isinstance(outcome["error"], ArchiveError), outcome["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows devices and streams")
@pytest.mark.parametrize("name", ["aux.md", "a.md:x"])
def test_device_and_stream_names_are_refused_on_windows(tmp_path: Path, name: str) -> None:
    dest = tmp_path / "dest"

    outcome = _extract_watched("zip", [("ok.md", b"ok"), (name, b"x")], dest)

    assert "error" in outcome, "extraction did not finish"
    assert isinstance(outcome["error"], ArchiveError), outcome["error"]
    assert not (dest / "a.md").exists()
    assert not os.path.exists(f"{dest / 'a.md'}:x")
