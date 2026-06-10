"""Safety tests for archive extraction helpers."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from apm_cli.utils.archive import ArchiveError, _extract_tar_gz, _extract_zip


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
