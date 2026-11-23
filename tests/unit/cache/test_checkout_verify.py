"""Cached and cloned checkouts are verified against their commit, trusting no object.

A checkout counts only if the commit re-hashes to its id, every reachable
tree re-hashes to its id, every working-tree file holds its blob's bytes
(through Git's own checkout filters), links hold their resolved targets, and
nothing untracked sits beside them.
"""

from __future__ import annotations

import os
import stat
import zlib
from pathlib import Path

import pytest

from apm_cli.cache.checkout_verify import (
    CheckoutVerificationError,
    forget_verdicts,
    require_verified_checkout,
    verify_checkout,
)
from apm_cli.cache.git_cache import GitCache
from tests.utils.git_link_fixture import (
    EXPECTED_PKG,
    build_link_origin,
    git,
    neutral_git_env,
    tree_bytes,
)

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


@pytest.fixture(autouse=True)
def _fresh_verdicts():
    forget_verdicts()
    yield
    forget_verdicts()


def _plain_repo(root: Path) -> tuple[Path, str]:
    """A one-commit repository whose objects are all loose."""
    repo = root / "plain"
    git("init", "-q", "-b", "main", "--template=", str(repo))
    (repo / "pkg").mkdir()
    (repo / "pkg" / "AGENTS.md").write_bytes(b"Package instructions\n")
    (repo / "README.md").write_bytes(b"readme\n")
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", "one")
    return repo, git("-C", str(repo), "rev-parse", "HEAD")


def _oid(repo: Path, spec: str) -> str:
    return git("-C", str(repo), "rev-parse", spec)


def _rewrite_loose(repo: Path, oid: str, kind: str, data: bytes) -> None:
    """Replace a loose object's content while keeping its (now false) id."""
    path = repo / ".git" / "objects" / oid[:2] / oid[2:]
    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    path.write_bytes(zlib.compress(f"{kind} {len(data)}\0".encode() + data))


def test_a_genuine_checkout_verifies(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)

    assert verify_checkout(repo, sha, env=neutral_git_env())


def test_a_tampered_working_file_with_head_intact_fails(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    (repo / "pkg" / "AGENTS.md").write_bytes(b"Injected instructions\n")

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    assert git("-C", str(repo), "rev-parse", "HEAD") == sha, "HEAD alone proves nothing"


def test_a_tampered_loose_blob_with_a_consistent_working_file_fails(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    blob = _oid(repo, "HEAD:pkg/AGENTS.md")
    forged = b"Injected instructions\n"
    _rewrite_loose(repo, blob, "blob", forged)
    (repo / "pkg" / "AGENTS.md").write_bytes(forged)

    assert git("-C", str(repo), "cat-file", "-p", blob) == forged.decode().strip()
    assert not verify_checkout(repo, sha, env=neutral_git_env())


def test_a_tampered_tree_object_fails(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    tree = _oid(repo, "HEAD:pkg")
    blob = bytes.fromhex(_oid(repo, "HEAD:README.md"))
    _rewrite_loose(repo, tree, "tree", b"100644 AGENTS.md\0" + blob)

    assert not verify_checkout(repo, sha, env=neutral_git_env())


def test_a_tampered_commit_object_fails(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    other = _oid(repo, "HEAD:pkg")
    _rewrite_loose(repo, sha, "commit", f"tree {other}\n\nforged\n".encode())

    assert not verify_checkout(repo, sha, env=neutral_git_env())


def test_an_untracked_file_fails_the_whole_checkout_but_not_outside_a_sparse_scope(
    tmp_path: Path,
) -> None:
    repo, sha = _plain_repo(tmp_path)
    (repo / "pkg" / "extra.md").write_bytes(b"untracked\n")
    (repo / "notes.txt").write_bytes(b"untracked\n")

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    (repo / "pkg" / "extra.md").unlink()
    assert verify_checkout(repo, sha, paths=["pkg"], env=neutral_git_env())


def test_a_linked_package_shard_verifies(tmp_path: Path) -> None:
    origin, sha = build_link_origin(tmp_path)

    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), sha, locked_sha=sha)

    assert verify_checkout(checkout, sha, env=neutral_git_env())
    assert tree_bytes(checkout / "pkg") == EXPECTED_PKG


def test_the_cache_rebuilds_a_tampered_shard_with_the_true_bytes(tmp_path: Path) -> None:
    origin, sha = build_link_origin(tmp_path)
    cache = GitCache(tmp_path / "cache")
    checkout = cache.get_checkout(str(origin), sha, locked_sha=sha)
    (checkout / "pkg" / "AGENTS.md").write_bytes(b"Injected instructions\n")
    (checkout / "pkg" / "untracked.md").write_bytes(b"never deployed\n")
    forget_verdicts()  # a later run

    healed = cache.get_checkout(str(origin), sha, locked_sha=sha)

    assert tree_bytes(healed / "pkg") == EXPECTED_PKG
    assert not (healed / "pkg" / "untracked.md").exists()


def test_a_read_only_reader_treats_a_tampered_shard_as_a_miss(tmp_path: Path) -> None:
    origin, sha = build_link_origin(tmp_path)
    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), sha, locked_sha=sha)
    (checkout / "pkg" / "AGENTS.md").write_bytes(b"Injected instructions\n")
    forget_verdicts()

    with pytest.raises(LookupError, match="does not match its commit"):
        GitCache(tmp_path / "cache", read_only=True).get_checkout(str(origin), sha, locked_sha=sha)
    assert (checkout / "pkg" / "AGENTS.md").read_bytes() == b"Injected instructions\n"


def test_an_empty_cache_yields_a_verified_checkout(tmp_path: Path) -> None:
    origin, sha = build_link_origin(tmp_path)

    checkout = GitCache(tmp_path / "empty-cache").get_checkout(str(origin), sha, locked_sha=sha)

    assert tree_bytes(checkout / "pkg") == EXPECTED_PKG
    assert verify_checkout(checkout, sha, env=neutral_git_env())


def test_a_direct_clone_is_refused_when_it_does_not_match(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    require_verified_checkout(repo, None, env=neutral_git_env())
    forget_verdicts()
    (repo / "pkg" / "AGENTS.md").write_bytes(b"Injected instructions\n")

    with pytest.raises(CheckoutVerificationError, match="does not match its commit"):
        require_verified_checkout(repo, sha, paths=["pkg"], env=neutral_git_env())


def test_only_a_full_object_id_verifies_and_an_abbreviation_means_head(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)

    assert not verify_checkout(repo, "", env=neutral_git_env())
    assert not verify_checkout(repo, sha[:12], env=neutral_git_env())
    require_verified_checkout(repo, sha[:12], env=neutral_git_env())
    with pytest.raises(CheckoutVerificationError):
        require_verified_checkout(repo, "0" * 12, env=neutral_git_env())
