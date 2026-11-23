"""Cached and cloned checkouts are verified against their commit, trusting no object.

A checkout counts only if the commit re-hashes to its id, every tree read
re-hashes to its id, every working-tree file holds its blob's bytes (or the
CRLF form the commit's own ``.gitattributes`` asks for, worked out without
running any Git filter), each file has its committed mode, links hold their
resolved targets, and nothing else sits beside them. Consumers then check the
bytes they copy or read against what verification accepted.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import zlib
from pathlib import Path

import pytest

from apm_cli.cache.checkout_verify import (
    CheckoutVerificationError,
    forget_verdicts,
    read_verified_file,
    require_verified_checkout,
    require_verified_copy,
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

pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-package-content-hash"),
]
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and shell")


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


def test_an_untracked_file_fails_with_or_without_a_scope(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    (repo / "notes.txt").write_bytes(b"untracked\n")

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    assert not verify_checkout(repo, sha, paths=["pkg"], env=neutral_git_env())
    (repo / "notes.txt").unlink()
    (repo / "README.md").unlink()  # outside the scope, a direct clone may omit it
    assert verify_checkout(repo, sha, paths=["pkg"], env=neutral_git_env())


def test_a_sparse_cone_holds_exactly_what_the_cone_checks_out(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    (repo / "other").mkdir()
    (repo / "other" / "x.md").write_bytes(b"x\n")
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", "two")
    sha = git("-C", str(repo), "rev-parse", "HEAD")

    assert not verify_checkout(repo, sha, paths=["pkg"], cone=True, env=neutral_git_env())
    (repo / "other" / "x.md").unlink()  # a tracked file the cone would not check out
    assert verify_checkout(repo, sha, paths=["pkg"], cone=True, env=neutral_git_env())
    (repo / "README.md").unlink()  # a root file the cone does check out
    assert not verify_checkout(repo, sha, paths=["pkg"], cone=True, env=neutral_git_env())


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


@_POSIX_ONLY
def test_no_git_filter_or_driver_runs_and_its_output_is_not_trusted(tmp_path: Path) -> None:
    repo, _sha = _plain_repo(tmp_path)
    (repo / ".gitattributes").write_bytes(b"*.md filter=x\n")
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", "attributes")
    sha = git("-C", str(repo), "rev-parse", "HEAD")
    marker = tmp_path / "marker"
    forged = b"Injected instructions\n"
    command = f"sh -c 'echo hit >> {marker.as_posix()}; printf \"Injected instructions\\\\n\"'"
    git("-C", str(repo), "config", "filter.x.smudge", command)
    git("-C", str(repo), "config", "filter.x.process", command)
    (repo / "pkg" / "AGENTS.md").write_bytes(forged)

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    assert not marker.exists(), "verification ran a filter driver"


def _attributes_repo(root: Path, attributes: bytes) -> tuple[Path, str]:
    repo, _sha = _plain_repo(root)
    (repo / ".gitattributes").write_bytes(attributes)
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", "attributes")
    return repo, git("-C", str(repo), "rev-parse", "HEAD")


def test_crlf_is_accepted_only_where_the_commit_asks_for_it(tmp_path: Path) -> None:
    repo, sha = _attributes_repo(tmp_path, b"pkg/*.md text eol=crlf\n")
    agents = repo / "pkg" / "AGENTS.md"

    agents.write_bytes(b"Package instructions\r\n")
    assert verify_checkout(repo, sha, env=neutral_git_env())
    agents.write_bytes(b"Package instructions\r\nInjected\r\n")
    assert not verify_checkout(repo, sha, env=neutral_git_env())
    agents.write_bytes(b"Package instructions\n")
    (repo / "README.md").write_bytes(b"readme\r\n")  # no eol=crlf for README.md
    assert not verify_checkout(repo, sha, env=neutral_git_env())


@pytest.mark.parametrize(
    "attributes",
    [b"pkg/*.md eol=crlf -text\n", b"pkg/*.md eol=crlf ident\n", b"pkg/** eol=crlf filter=lfs\n"],
    ids=["text-unset", "ident", "filter"],
)
def test_paths_with_a_transform_attribute_must_hold_their_exact_blob(
    tmp_path: Path, attributes: bytes
) -> None:
    repo, sha = _attributes_repo(tmp_path, attributes)
    (repo / "pkg" / "AGENTS.md").write_bytes(b"Package instructions\r\n")

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    (repo / "pkg" / "AGENTS.md").write_bytes(b"Package instructions\n")
    assert verify_checkout(repo, sha, env=neutral_git_env())


@_POSIX_ONLY
def test_each_file_has_its_committed_mode(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    agents = repo / "pkg" / "AGENTS.md"
    agents.chmod(0o755)

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    agents.chmod(0o644)
    assert verify_checkout(repo, sha, env=neutral_git_env())


def test_a_copy_is_checked_against_the_bytes_verification_accepted(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    require_verified_checkout(repo, sha, env=neutral_git_env())
    dest = tmp_path / "dest"
    shutil.copytree(repo / "pkg", dest)
    require_verified_copy(dest, repo, sha, "pkg")

    (repo / "pkg" / "AGENTS.md").write_bytes(b"Injected instructions\n")  # after verification
    shutil.copy2(repo / "pkg" / "AGENTS.md", dest / "AGENTS.md")
    with pytest.raises(CheckoutVerificationError, match="verified bytes"):
        require_verified_copy(dest, repo, sha, "pkg")
    with pytest.raises(CheckoutVerificationError, match="verified bytes"):
        read_verified_file(repo, "pkg/AGENTS.md", sha)


def _forge_loose(git_dir: Path, oid: str, kind: str, data: bytes) -> None:
    """Replace one loose object with other content under the same id (never in place)."""
    path = git_dir / "objects" / oid[:2] / oid[2:]
    path.unlink()
    path.write_bytes(zlib.compress(f"{kind} {len(data)}\0".encode() + data))


def _poisoned_cache(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    """A cached checkout whose file and whose bare's blob both carry forged bytes."""
    origin, sha = _plain_repo(tmp_path)
    cache_root = tmp_path / "cache"
    checkout = GitCache(cache_root).get_checkout(str(origin), sha, locked_sha=sha)
    bare = cache_root / "git" / "db_v1" / checkout.parent.parent.name
    blob = _oid(origin, "HEAD:pkg/AGENTS.md")
    forged = b"Injected instructions\n"
    _forge_loose(bare, blob, "blob", forged)
    (checkout / "pkg" / "AGENTS.md").write_bytes(forged)
    forget_verdicts()  # a later run
    return origin, sha, cache_root, checkout


def test_a_poisoned_bare_is_never_the_source_of_a_rebuild(tmp_path: Path) -> None:
    origin, sha, cache_root, _checkout = _poisoned_cache(tmp_path)

    healed = GitCache(cache_root).get_checkout(str(origin), sha, locked_sha=sha)

    assert (healed / "pkg" / "AGENTS.md").read_bytes() == b"Package instructions\n"
    assert verify_checkout(healed, sha, env=neutral_git_env())


def test_an_unhealable_cache_entry_is_refused(tmp_path: Path) -> None:
    origin, sha, cache_root, checkout = _poisoned_cache(tmp_path)
    shutil.rmtree(origin)  # the commit is no longer available anywhere else

    with pytest.raises(RuntimeError, match=r"Failed to clone|does not match its commit"):
        GitCache(cache_root).get_checkout(str(origin), sha, locked_sha=sha)

    assert not checkout.exists(), "the unverifiable checkout was kept"


def test_a_truncated_copy_is_refused(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    (repo / "pkg" / "SKILL.md").write_bytes(b"skill\n")
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", "two")
    sha = git("-C", str(repo), "rev-parse", "HEAD")
    require_verified_checkout(repo, sha, env=neutral_git_env())
    dest = tmp_path / "dest"
    shutil.copytree(repo / "pkg", dest)
    (dest / "SKILL.md").unlink()

    with pytest.raises(CheckoutVerificationError, match="missing from the copy"):
        require_verified_copy(dest, repo, sha, "pkg")


@_POSIX_ONLY
def test_an_untracked_directory_link_never_verifies(tmp_path: Path) -> None:
    repo, sha = _plain_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"not from the commit\n")
    (repo / "pkg" / "linked").symlink_to(outside, target_is_directory=True)

    assert not verify_checkout(repo, sha, env=neutral_git_env())
    assert not verify_checkout(repo, sha, paths=["pkg"], env=neutral_git_env())


def test_an_inherited_licence_must_hold_its_verified_bytes(tmp_path: Path) -> None:
    from apm_cli.deps.carried_license import checked_license

    repo, _sha = _plain_repo(tmp_path)
    (repo / "LICENSE").write_bytes(b"MIT\n")
    git("-C", str(repo), "add", "-A")
    git("-C", str(repo), "commit", "-q", "-m", "licence")
    sha = git("-C", str(repo), "rev-parse", "HEAD")
    require_verified_checkout(repo, sha, env=neutral_git_env())
    assert checked_license(repo, "pkg", sha).data == b"MIT\n"

    (repo / "LICENSE").write_bytes(b"Something else\n")  # after verification
    with pytest.raises(CheckoutVerificationError, match="verified bytes"):
        checked_license(repo, "pkg", sha)


def test_a_quoted_attribute_pattern_keeps_the_other_rules(tmp_path: Path) -> None:
    repo, sha = _attributes_repo(tmp_path, b'"pkg/a b.md" -text\npkg/*.md text eol=crlf\n')

    (repo / "pkg" / "AGENTS.md").write_bytes(b"Package instructions\r\n")
    assert verify_checkout(repo, sha, env=neutral_git_env())


def test_a_scope_that_is_itself_a_directory_link_verifies(tmp_path: Path) -> None:
    origin, sha = build_link_origin(tmp_path)

    checkout = GitCache(tmp_path / "cache").get_checkout(
        str(origin), sha, locked_sha=sha, sparse_paths=["pkg/shared"]
    )

    shared = {k[len("shared/") :]: v for k, v in EXPECTED_PKG.items() if k.startswith("shared/")}
    assert tree_bytes(checkout / "pkg" / "shared") == shared


def test_ambient_git_location_variables_do_not_redirect_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _plain_repo(tmp_path)
    other, _ = _attributes_repo(tmp_path / "other", b"* -text\n")  # a different commit
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(other / ".git" / "objects"))

    assert verify_checkout(repo, sha)
