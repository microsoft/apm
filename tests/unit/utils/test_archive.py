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


# -- Tar decompression is bounded, headers included (#4973) ----------------------
# tarfile reads a pax or GNU long-name header into memory at the size the header
# declares, before APM sees a member. The decompressed stream is bounded by the
# uncompressed cap plus a small header allowance; past it, the existing size
# error is raised before the read.

_DECLARED_HEADER_BYTES = 4 * 1024 * 1024 * 1024
_HEADER_TYPES = {
    "pax": tarfile.XHDTYPE,
    "pax-global": tarfile.XGLTYPE,
    "gnu-longname": tarfile.GNUTYPE_LONGNAME,
    "gnu-longlink": tarfile.GNUTYPE_LONGLINK,
}


def _gzip(raw: bytes) -> bytes:
    import gzip

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(raw)
    return buf.getvalue()


def _oversized_header_tar_gz(kind: str) -> bytes:
    info = tarfile.TarInfo("././@Header")
    info.type = _HEADER_TYPES[kind]
    info.size = _DECLARED_HEADER_BYTES
    return _gzip(info.tobuf(format=tarfile.USTAR_FORMAT) + b"x" * 512)


def _with_checksum(block: bytearray) -> bytes:
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def _endless_sparse_tar_gz(extension_blocks: int) -> bytes:
    """An old-GNU sparse member followed by *extension_blocks* empty extension headers."""
    info = tarfile.TarInfo("sparse.bin")
    info.type = tarfile.GNUTYPE_SPARSE
    header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    header[482] = 1
    extension = bytearray(512)
    extension[504] = 1
    blocks = [_with_checksum(header)] + [bytes(extension)] * (extension_blocks - 1)
    blocks.append(bytes(512))
    return _gzip(b"".join(blocks) + bytes(1024))


def _peak_while(action) -> tuple[BaseException | None, int]:
    import tracemalloc

    tracemalloc.start()
    try:
        action()
        error = None
    except BaseException as exc:
        error = exc
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return error, peak


@pytest.mark.parametrize("kind", sorted(_HEADER_TYPES))
def test_oversized_tar_header_is_refused_before_it_is_read(tmp_path: Path, kind: str) -> None:
    data = _oversized_header_tar_gz(kind)

    error, peak = _peak_while(lambda: _extract_tar_gz(data, str(tmp_path / "out")))

    assert isinstance(error, ArchiveError), error
    assert "exceeds size limit" in str(error)
    assert peak < 32 * 1024 * 1024
    assert not (tmp_path / "out").exists()


def test_oversized_tar_header_is_refused_for_a_tar_gz_file(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(_oversized_header_tar_gz("pax"))

    error, peak = _peak_while(
        lambda: archive_mod._extract_tar_gz_file(archive, str(tmp_path / "out"))
    )

    assert isinstance(error, ArchiveError), error
    assert "exceeds size limit" in str(error)
    assert peak < 32 * 1024 * 1024
    assert not (tmp_path / "out").exists()


def test_oversized_tar_header_is_refused_for_a_registry_tarball(tmp_path: Path) -> None:
    import hashlib

    from apm_cli.deps.registry.extractor import UnsafeTarballError, extract_tarball

    data = _oversized_header_tar_gz("gnu-longname")
    digest = hashlib.sha256(data).hexdigest()

    error, peak = _peak_while(lambda: extract_tarball(data, digest, tmp_path / "out"))

    assert isinstance(error, UnsafeTarballError), error
    assert "exceeds size limit" in str(error)
    assert peak < 32 * 1024 * 1024
    assert not (tmp_path / "out").exists()


def _extract_capped(data: bytes, dest: Path, cap: int) -> list[str]:
    with archive_mod.open_tar_gz(fileobj=io.BytesIO(data), max_uncompressed=cap) as tar:
        return archive_mod.safe_extract_tar(
            tar, dest, max_uncompressed=cap, error_type=ArchiveError
        )


def test_endless_sparse_headers_are_refused_within_the_bound(tmp_path: Path) -> None:
    data = _endless_sparse_tar_gz(40 * 1024)

    with pytest.raises(ArchiveError, match="exceeds size limit"):
        _extract_capped(data, tmp_path / "out", 1024)
    assert not (tmp_path / "out").exists()


def test_many_small_global_headers_are_refused_within_the_bound(tmp_path: Path) -> None:
    header = tarfile.TarInfo._create_pax_generic_header(
        {"comment": "c" * (1024 * 1024)}, tarfile.XGLTYPE, "utf-8"
    )
    member = tarfile.TarInfo("ok.md")
    raw = header * 40 + member.tobuf(format=tarfile.USTAR_FORMAT) + bytes(1024)

    with pytest.raises(ArchiveError, match="exceeds size limit"):
        _extract_capped(_gzip(raw), tmp_path / "out", 1024)
    assert not (tmp_path / "out").exists()


def test_large_pax_header_within_the_caps_extracts(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo("plugin/SKILL.md")
        info.size = 7
        info.pax_headers = {"comment": "c" * (2 * 1024 * 1024)}
        tf.addfile(info, io.BytesIO(b"content"))

    extracted = _extract_tar_gz(buf.getvalue(), str(tmp_path / "out"))

    assert extracted == ["plugin/SKILL.md"]
    assert (tmp_path / "out" / "plugin" / "SKILL.md").read_bytes() == b"content"


def test_archive_near_the_cap_extracts(tmp_path: Path) -> None:
    cap = 64 * 1024
    entries = {f"plugin/f{i}.md": b"x" for i in range(30)}
    entries["plugin/big.bin"] = b"x" * (cap - 30)

    extracted = _extract_capped(_tar_gz_bytes(entries), tmp_path / "out", cap)

    assert len(extracted) == 31
    assert (tmp_path / "out" / "plugin" / "big.bin").stat().st_size == cap - 30


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
