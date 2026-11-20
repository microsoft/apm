"""Opt-in native fingerprints preserve the historical package hash contract."""

import hashlib
import os
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_native_bundle_hash, compute_package_hash
from apm_cli.utils.path_security import PathTraversalError

pytestmark = pytest.mark.component


def test_native_hash_includes_publisher_cache_assets_without_changing_legacy(
    tmp_path: Path,
) -> None:
    """Publisher cache-named files belong to native payload, not legacy hashes."""
    (tmp_path / "file.txt").write_bytes(b"raw\r\npublisher bytes")
    legacy = "sha256:" + hashlib.sha256(b"file.txtraw\r\npublisher bytes").hexdigest()
    before = compute_native_bundle_hash(tmp_path)
    asset = tmp_path / "resources/__pycache__/asset.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"\x00first")
    added = compute_native_bundle_hash(tmp_path)
    assert added != before
    asset.write_bytes(b"\x00second")
    assert compute_native_bundle_hash(tmp_path) != added
    assert compute_package_hash(tmp_path, framed=False) == legacy


@pytest.mark.skipif(os.name == "nt", reason="Git owner executable-bit observation requires POSIX")
def test_native_hash_observes_only_git_executable_mode(tmp_path: Path) -> None:
    """Only the Git executable bit, not unrelated permission bits, is identity."""
    payload = tmp_path / "inert.bin"
    payload.write_bytes(b"not executable code")
    payload.chmod(0o644)
    before = compute_native_bundle_hash(tmp_path)
    legacy = compute_package_hash(tmp_path, framed=False)
    payload.chmod(0o755)
    executable = compute_native_bundle_hash(tmp_path)
    assert executable != before
    payload.chmod(0o744)
    assert compute_native_bundle_hash(tmp_path) == executable
    assert compute_package_hash(tmp_path, framed=False) == legacy


def test_native_hash_excludes_transport_but_not_nested_pin(tmp_path: Path) -> None:
    """Only Git transport and the root APM marker are excluded."""
    (tmp_path / "payload").write_bytes(b"publisher")
    before = compute_native_bundle_hash(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("transport metadata\n")
    (tmp_path / ".apm-pin").write_text("APM transport marker\n")
    assert compute_native_bundle_hash(tmp_path) == before
    nested = tmp_path / "resources/.apm-pin"
    nested.parent.mkdir()
    nested.write_bytes(b"publisher-owned nested file")
    assert compute_native_bundle_hash(tmp_path) != before


@pytest.mark.skipif(
    os.name == "nt", reason="Native Windows symlink capability is separately qualified"
)
@pytest.mark.parametrize("position", ["root", "payload", "dangling"])
def test_native_hash_refuses_publisher_symlinks(tmp_path: Path, position: str) -> None:
    """A native fingerprint cannot attest a publisher link as regular payload."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"publisher")
    if position == "root":
        link = tmp_path / "linked-root"
        link.symlink_to(source, target_is_directory=True)
        hashed = link
    else:
        (source / "linked-payload").symlink_to(
            source / ("payload" if position == "payload" else "missing")
        )
        hashed = source
    with pytest.raises(PathTraversalError, match="refuses symlink"):
        compute_native_bundle_hash(hashed)


@pytest.mark.skipif(os.name == "nt", reason="Git owner executable-bit observation requires POSIX")
def test_native_pins_keep_their_values_through_the_shared_framing(tmp_path: Path) -> None:
    """Hash framing hardening must not move existing native plugin pins (#4000)."""
    from apm_cli.utils.content_hash import compute_native_plugin_hash, compute_native_plugin_pin

    (tmp_path / "run.sh").write_bytes(b"#!/bin/sh\necho ok\n")
    (tmp_path / "run.sh").chmod(0o755)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_bytes(b"alpha\r\n")
    (tmp_path / "docs" / "a.md").chmod(0o644)
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_bytes(b'{"name":"p"}\n')

    with_mode = "sha256:500baca30a4b08c81d71b912c325ed68a54c51855b157bdca0be271da7de7892"
    assert compute_native_bundle_hash(tmp_path) == with_mode
    assert compute_native_plugin_hash(tmp_path) == with_mode
    assert compute_native_plugin_pin(tmp_path) == (
        "sha256:84e6044326b475f5c05cc423b9dc0a907ee287ce45cda28be9bcb24ea145a192"
    )
