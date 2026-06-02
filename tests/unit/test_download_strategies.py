"""Tests for backend-specific download delegates."""

from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.deps.download_strategies import DownloadDelegate


def _zip_with_executable_script() -> bytes:
    """Build a GitHub-style archive containing an executable script."""
    root_info = zipfile.ZipInfo("repo-main/")
    root_info.external_attr = (stat.S_IFDIR | 0o755) << 16
    script_info = zipfile.ZipInfo("repo-main/scripts/do-driver")
    script_info.external_attr = (stat.S_IFREG | 0o755) << 16

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(root_info, b"")
        zf.writestr(script_info, b"#!/bin/sh\n")
    return buf.getvalue()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not honor POSIX execute bits; st_mode & 0o111 is not preserved",
)
def test_artifactory_archive_preserves_executable_bits(tmp_path):
    response = SimpleNamespace(status_code=200, content=_zip_with_executable_script())
    host = SimpleNamespace(registry_config=None, artifactory_token=None)
    host._resilient_get = lambda *args, **kwargs: response

    with patch(
        "apm_cli.deps.download_strategies.build_artifactory_archive_url",
        return_value=["https://art.example/archive.zip"],
    ):
        DownloadDelegate(host).download_artifactory_archive(
            "art.example",
            "apm",
            "owner",
            "repo",
            "main",
            tmp_path,
        )

    script = tmp_path / "scripts" / "do-driver"
    assert script.read_bytes() == b"#!/bin/sh\n"
    assert os.stat(script).st_mode & 0o111 == 0o111
