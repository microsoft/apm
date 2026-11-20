"""Unit tests for apm_cli.drift -- pure drift-detection helpers.

These tests cover detect_ref_change, the install orphan decision, detect_config_drift,
and build_download_ref.  All four functions are stateless and side-effect-free
so no I/O or network access is required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional  # noqa: F401, UP035
from unittest.mock import MagicMock, call, patch  # noqa: F401

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-locked-copy-validity", "P-OWN-read-only-lockfile-path"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _LockedDep(LockedDependency):
    """Use the lockfile source contract with a convenient default identity."""

    repo_url: str = "owner/repo"


def _dep(repo_url: str = "owner/repo", reference: str | None = None) -> DependencyReference:
    return DependencyReference(repo_url=repo_url, reference=reference)


# ---------------------------------------------------------------------------
# detect_ref_change
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the install orphan decision
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# detect_config_drift
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_download_ref
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# locked_copy_proof / locked_copy_is_stale (#3884)
# ---------------------------------------------------------------------------


from apm_cli.drift import (  # noqa: E402
    LOCKED_COPY_CONTENT_HASH,
    LOCKED_COPY_GIT_HEAD,
    LOCKED_COPY_PIN,
    locked_copy_is_stale,
    locked_copy_proof,
    should_force_ref_recheck,
)

_LOCKED = "a" * 40


def _copy(tmp_path, body: str = "v2", marker: str | None = None):
    from apm_cli.install.cache_pin import write_marker

    path = tmp_path / "copy"
    path.mkdir()
    (path / "apm.yml").write_text(f"name: p\nversion: {body}\n", encoding="utf-8")
    if marker is not None:
        write_marker(path, marker)
    return path


def _hash_of(tmp_path, body: str) -> str:
    from apm_cli.utils.content_hash import compute_package_hash

    path = tmp_path / f"hash-{body}"
    path.mkdir()
    (path / "apm.yml").write_text(f"name: p\nversion: {body}\n", encoding="utf-8")
    return compute_package_hash(path)


@pytest.mark.parametrize(
    ("commit", "hashed_as", "marker", "proof", "stale"),
    [
        (_LOCKED, "v1", _LOCKED, LOCKED_COPY_PIN, False),
        (_LOCKED, "v2", "b" * 40, LOCKED_COPY_CONTENT_HASH, False),
        (_LOCKED, "v1", "b" * 40, None, True),
        (_LOCKED, None, None, None, False),
        (_LOCKED, None, "b" * 40, None, True),
        (None, None, None, None, False),
        ("cached", "v2", _LOCKED, LOCKED_COPY_CONTENT_HASH, False),
    ],
    ids=[
        "pin-match",
        "pin-mismatch-hash-match",
        "pin-and-hash-mismatch",
        "legacy-unmarked-unhashed",
        "unhashed-marker-mismatch",
        "no-evidence",
        "cached-sentinel",
    ],
)
def test_locked_copy_proof_branches(tmp_path, commit, hashed_as, marker, proof, stale) -> None:
    locked = _LockedDep(
        resolved_commit=commit,
        content_hash=_hash_of(tmp_path, hashed_as) if hashed_as else None,
    )
    path = _copy(tmp_path, marker=marker)

    assert locked_copy_proof(locked, path, trust_marker=True) == proof
    assert locked_copy_is_stale(locked, path) is stale
    assert should_force_ref_recheck(_dep(), locked, update_refs=False, package_path=path) is stale


def test_locked_copy_proof_lets_a_readable_git_head_decide(tmp_path) -> None:
    import subprocess

    path = _copy(tmp_path, marker=None)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"),
    ):
        subprocess.run(("git", *args), cwd=path, env=env, check=True)
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=path, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert locked_copy_proof(_LockedDep(resolved_commit=head), path) == LOCKED_COPY_GIT_HEAD
    assert locked_copy_is_stale(_LockedDep(resolved_commit=head), path) is False
    # An unmarked, unhashed checkout at another commit is refreshed first.
    other = _LockedDep(resolved_commit=_LOCKED)
    assert locked_copy_is_stale(other, path) is True
    assert should_force_ref_recheck(_dep(), other, update_refs=False, package_path=path) is True
    # A matching marker never outranks a readable HEAD naming another commit.
    from apm_cli.install.cache_pin import write_marker

    write_marker(path, _LOCKED)
    assert locked_copy_proof(_LockedDep(resolved_commit=_LOCKED), path, trust_marker=True) is None


def test_pin_never_proves_bytes_for_deployment(tmp_path) -> None:
    path = _copy(tmp_path, marker=_LOCKED)
    unhashed = _LockedDep(resolved_commit=_LOCKED)
    hashed = _LockedDep(resolved_commit=_LOCKED, content_hash=_hash_of(tmp_path, "v2"))

    assert locked_copy_proof(unhashed, path, trust_marker=True) == LOCKED_COPY_PIN
    assert locked_copy_proof(unhashed, path) is None
    assert locked_copy_proof(hashed, path) == LOCKED_COPY_CONTENT_HASH
