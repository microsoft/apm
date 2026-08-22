"""Reproducible archive writer for produced Agent Plugin bundles."""

from __future__ import annotations

import gzip
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path

from ..utils.path_security import ensure_path_within

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _archive_files(bundle_dir: Path) -> list[Path]:
    """Return regular non-symlink bundle files in stable order."""
    return sorted(
        path for path in bundle_dir.rglob("*") if path.is_file() and not path.is_symlink()
    )


def _normalized_mode(path: Path) -> int:
    """Preserve executable intent while normalizing all other mode metadata."""
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _archive_name(bundle_dir: Path, path: Path) -> str:
    return f"{bundle_dir.name}/{path.relative_to(bundle_dir).as_posix()}"


def _write_zip(bundle_dir: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in _archive_files(bundle_dir):
            info = zipfile.ZipInfo(_archive_name(bundle_dir, path), _ZIP_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | _normalized_mode(path)) << 16
            with path.open("rb") as source, archive.open(info, "w") as member:
                shutil.copyfileobj(source, member)


def _write_tar_gz(bundle_dir: Path, archive_path: Path) -> None:
    with open(archive_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for path in _archive_files(bundle_dir):
                    info = tarfile.TarInfo(_archive_name(bundle_dir, path))
                    info.size = path.stat().st_size
                    info.mode = _normalized_mode(path)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with path.open("rb") as source:
                        archive.addfile(info, source)


def write_reproducible_archive(
    bundle_dir: Path,
    archive_path: Path,
    archive_format: str,
) -> None:
    """Write one deterministic zip or tar.gz archive."""
    ensure_path_within(archive_path, archive_path.parent)
    if archive_format == "tar.gz":
        _write_tar_gz(bundle_dir, archive_path)
        return
    if archive_format == "zip":
        _write_zip(bundle_dir, archive_path)
        return
    raise ValueError(f"Unsupported reproducible archive format: {archive_format!r}")
