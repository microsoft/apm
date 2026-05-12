"""Regression tests for Windows absolute path rejection in tar extraction.

Verifies that _looks_like_legacy_apm_bundle() and the unpacker reject
tar members with Windows absolute paths (e.g., D:/...) before extraction.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest


class TestLegacyBundleProbeRejectsWindowsAbsolutePaths:
    """Verify _looks_like_legacy_apm_bundle rejects Windows absolute paths."""

    def _make_tarball_with_members(self, tmp_path: Path, members: list[tuple[str, str]]) -> Path:
        """Create a .tar.gz with the given (name, content) members."""
        tarball_path = tmp_path / "malicious.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            for name, content in members:
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return tarball_path

    def test_rejects_windows_drive_letter_path(self, tmp_path: Path) -> None:
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                ("bundle/apm.lock.yaml", "packages: []"),
                ("D:/evil/payload.txt", "malicious content"),
            ],
        )

        assert _looks_like_legacy_apm_bundle(tarball) is False

    def test_rejects_windows_unc_path(self, tmp_path: Path) -> None:
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                ("bundle/apm.lock.yaml", "packages: []"),
                ("//server/share/payload.txt", "malicious content"),
            ],
        )

        assert _looks_like_legacy_apm_bundle(tarball) is False

    def test_rejects_unix_absolute_path(self, tmp_path: Path) -> None:
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                ("bundle/apm.lock.yaml", "packages: []"),
                ("/etc/passwd", "malicious content"),
            ],
        )

        assert _looks_like_legacy_apm_bundle(tarball) is False

    def test_rejects_dot_dot_traversal(self, tmp_path: Path) -> None:
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                ("bundle/apm.lock.yaml", "packages: []"),
                ("bundle/../../etc/passwd", "malicious content"),
            ],
        )

        assert _looks_like_legacy_apm_bundle(tarball) is False

    def test_accepts_valid_legacy_bundle(self, tmp_path: Path) -> None:
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                ("bundle/apm.lock.yaml", "packages: []"),
                ("bundle/README.md", "hello"),
            ],
        )

        assert _looks_like_legacy_apm_bundle(tarball) is True

    def test_no_file_created_outside_temp(self, tmp_path: Path) -> None:
        """Ensure no file is created at the Windows absolute path."""
        from apm_cli.bundle.local_bundle import _looks_like_legacy_apm_bundle

        escape_target = tmp_path / "outside" / "payload.txt"

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                ("bundle/apm.lock.yaml", "packages: []"),
                (str(escape_target), "should not be written"),
            ],
        )

        _looks_like_legacy_apm_bundle(tarball)

        assert not escape_target.exists()


class TestUnpackerRejectsWindowsAbsolutePaths:
    """Verify unpacker rejects Windows absolute paths in tar members."""

    def _make_tarball_with_members(self, tmp_path: Path, members: list[tuple[str, str]]) -> Path:
        """Create a .tar.gz with the given (name, content) members."""
        tarball_path = tmp_path / "malicious.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            for name, content in members:
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return tarball_path

    def test_rejects_windows_drive_letter_in_unpack(self, tmp_path: Path) -> None:
        from apm_cli.bundle.unpacker import unpack_bundle

        tarball = self._make_tarball_with_members(
            tmp_path,
            [
                (
                    "bundle/apm.lock.yaml",
                    "packages: []\ndeployed_files:\n  README.md: {hash: abc}",
                ),
                ("D:/evil/payload.txt", "malicious content"),
            ],
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with pytest.raises(ValueError, match=r"path-traversal"):
            unpack_bundle(tarball, output_dir)
