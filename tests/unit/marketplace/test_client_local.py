"""Unit coverage for the ``_fetch_local`` marketplace fetcher.

Covers:
- bare-repo path (mocked ``git show``)
- working-dir path (direct read)
- traversal in ``source.path`` rejected
- symlink-escape from working-dir read raises ``MarketplaceFetchError``
- invalid ``ref`` rejected by ``_validate_ref``
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.marketplace.client import (
    _fetch_local,
    _fetch_local_direct_read,
    _validate_ref,
)
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource


def _local_source(name: str, path: Path, ref: str = "main") -> MarketplaceSource:
    return MarketplaceSource(name=name, url=f"file://{path}", ref=ref)


def test_fetch_local_bare_repo_via_git_show(tmp_path: Path) -> None:
    """Bare repo: ``git show`` returns blob content, parsed as JSON."""
    bare = tmp_path / "mkt.git"
    bare.mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main")
    (bare / "objects").mkdir()
    manifest = {"name": "acme", "plugins": []}

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(manifest).encode(), stderr=b""
    )
    with patch("apm_cli.marketplace.client.subprocess.run", return_value=completed) as run_mock:
        result = _fetch_local(_local_source("acme", bare), "marketplace.json")

    assert result == manifest
    args = run_mock.call_args.args[0]
    assert args[0] == "git"
    assert "--git-dir" in args
    assert "core.hooksPath=/dev/null" in args
    assert "main:marketplace.json" in args


def test_fetch_local_working_dir_direct_read(tmp_path: Path) -> None:
    """Working-dir layout: read marketplace.json directly from disk."""
    repo = tmp_path / "mkt"
    repo.mkdir()
    manifest = {"name": "acme", "plugins": []}
    (repo / "marketplace.json").write_text(json.dumps(manifest))

    result = _fetch_local(_local_source("acme", repo), "marketplace.json")
    assert result == manifest


def test_fetch_local_file_direct_read(tmp_path: Path) -> None:
    """Anthropic local file shape reads a marketplace.json file directly."""
    manifest_file = tmp_path / "marketplace.json"
    manifest = {"name": "acme", "plugins": []}
    manifest_file.write_text(json.dumps(manifest))

    result = _fetch_local(_local_source("acme", manifest_file), "")
    assert result == manifest


def test_fetch_local_missing_path_raises(tmp_path: Path) -> None:
    """Path that does not exist raises ``MarketplaceFetchError``."""
    with pytest.raises(MarketplaceFetchError, match="does not exist"):
        _fetch_local(_local_source("acme", tmp_path / "missing"), "marketplace.json")


def test_fetch_local_symlink_escape_blocked(tmp_path: Path) -> None:
    """Symlink that points outside the repo root must be rejected."""
    repo = tmp_path / "mkt"
    repo.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"leaked": true}')
    link = repo / "marketplace.json"
    os.symlink(secret, link)

    with pytest.raises(MarketplaceFetchError, match="escapes marketplace root"):
        _fetch_local_direct_read(_local_source("acme", repo), "marketplace.json", repo.resolve())


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        "-rf",
        "main; rm -rf /",
        "main:other",
        "main with space",
        "..",
    ],
)
def test_validate_ref_rejects_unsafe_inputs(bad_ref: str) -> None:
    with pytest.raises(MarketplaceFetchError, match="Invalid git ref"):
        _validate_ref(bad_ref, "acme")


@pytest.mark.parametrize(
    "good_ref",
    [
        "main",
        "v1.2.3",
        "feature/local-mkt",
        "release-2024.01",
        "abc123",
    ],
)
def test_validate_ref_accepts_safe_inputs(good_ref: str) -> None:
    assert _validate_ref(good_ref, "acme") == good_ref


def test_fetch_local_git_show_returns_none_when_path_missing(tmp_path: Path) -> None:
    """``git show`` reports 'does not exist' -> fetcher returns ``None`` so caller can probe next path."""
    bare = tmp_path / "mkt.git"
    bare.mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main")
    (bare / "objects").mkdir()

    completed = subprocess.CompletedProcess(
        args=[],
        returncode=128,
        stdout=b"",
        stderr=b"fatal: path 'marketplace.json' does not exist in 'main'",
    )
    with patch("apm_cli.marketplace.client.subprocess.run", return_value=completed):
        result = _fetch_local(_local_source("acme", bare), "marketplace.json")

    assert result is None


def test_fetch_local_traversal_in_file_path_is_blocked(tmp_path: Path) -> None:
    """Working-dir read with traversal segments in file_path must be rejected."""
    repo = tmp_path / "mkt"
    repo.mkdir()
    (tmp_path / "secret.json").write_text('{"leaked": true}')

    with pytest.raises(MarketplaceFetchError, match="escapes marketplace root"):
        _fetch_local_direct_read(_local_source("acme", repo), "../secret.json", repo.resolve())
