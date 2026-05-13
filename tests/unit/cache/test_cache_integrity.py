"""Unit tests for cache.integrity module.

Covers _read_head_sha and verify_checkout_sha across all .git layouts:
- .git directory with ref HEAD
- .git directory with detached HEAD
- .git directory with packed-refs fallback
- .git file (worktree gitdir indirection)
- missing / malformed layouts
"""

from apm_cli.cache.integrity import _read_head_sha, verify_checkout_sha

_SHA = "a" * 40
_SHA2 = "b" * 40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_dir(tmp_path, sha=_SHA):
    """Create a standard .git directory with detached HEAD."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(sha + "\n")
    return tmp_path


def _make_git_dir_with_ref(tmp_path, sha=_SHA, branch="refs/heads/main"):
    """Create a standard .git directory with a symbolic ref HEAD."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(f"ref: {branch}\n")
    ref_path = git_dir / branch
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(sha + "\n")
    return tmp_path


# ---------------------------------------------------------------------------
# _read_head_sha: .git is a directory, detached HEAD
# ---------------------------------------------------------------------------


class TestReadHeadShaDetachedHead:
    def test_returns_sha_for_detached_head(self, tmp_path):
        checkout = _make_git_dir(tmp_path, sha=_SHA)
        assert _read_head_sha(checkout) == _SHA

    def test_normalises_uppercase_sha(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text(_SHA.upper() + "\n")
        assert _read_head_sha(tmp_path) == _SHA.lower()

    def test_returns_none_for_non_sha_detached(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("not-a-sha\n")
        assert _read_head_sha(tmp_path) is None

    def test_returns_none_when_head_missing(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert _read_head_sha(tmp_path) is None


# ---------------------------------------------------------------------------
# _read_head_sha: .git is a directory, symbolic ref HEAD
# ---------------------------------------------------------------------------


class TestReadHeadShaSymbolicRef:
    def test_follows_symbolic_ref(self, tmp_path):
        checkout = _make_git_dir_with_ref(tmp_path, sha=_SHA)
        assert _read_head_sha(checkout) == _SHA

    def test_returns_none_when_ref_file_missing_and_no_packed_refs(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/missing\n")
        assert _read_head_sha(tmp_path) is None

    def test_falls_back_to_packed_refs(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        packed = git_dir / "packed-refs"
        packed.write_text(f"# pack-refs with: peeled fully-peeled sorted\n{_SHA} refs/heads/main\n")
        assert _read_head_sha(tmp_path) == _SHA

    def test_packed_refs_ignores_peeled_lines(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        packed = git_dir / "packed-refs"
        packed.write_text(f"{_SHA} refs/heads/main\n^{_SHA2}\n{_SHA2} refs/heads/other\n")
        assert _read_head_sha(tmp_path) == _SHA

    def test_packed_refs_no_match_returns_none(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        packed = git_dir / "packed-refs"
        packed.write_text(f"{_SHA} refs/heads/other\n")
        assert _read_head_sha(tmp_path) is None


# ---------------------------------------------------------------------------
# _read_head_sha: .git is a file (worktree)
# ---------------------------------------------------------------------------


class TestReadHeadShaWorktree:
    def test_follows_gitdir_indirection(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        real_git = tmp_path / "real_git"
        real_git.mkdir()
        (real_git / "HEAD").write_text(_SHA + "\n")
        (worktree / ".git").write_text(f"gitdir: {real_git}\n")
        assert _read_head_sha(worktree) == _SHA

    def test_gitdir_file_without_gitdir_prefix_returns_none(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("some random content\n")
        assert _read_head_sha(checkout) is None


# ---------------------------------------------------------------------------
# _read_head_sha: missing or empty .git
# ---------------------------------------------------------------------------


class TestReadHeadShaMissing:
    def test_returns_none_when_no_git_dir_or_file(self, tmp_path):
        assert _read_head_sha(tmp_path) is None

    def test_returns_none_for_nonexistent_checkout(self, tmp_path):
        assert _read_head_sha(tmp_path / "does_not_exist") is None


# ---------------------------------------------------------------------------
# verify_checkout_sha
# ---------------------------------------------------------------------------


class TestVerifyCheckoutSha:
    def test_returns_true_when_sha_matches(self, tmp_path):
        checkout = _make_git_dir(tmp_path, sha=_SHA)
        assert verify_checkout_sha(checkout, _SHA) is True

    def test_returns_false_when_sha_mismatches(self, tmp_path):
        checkout = _make_git_dir(tmp_path, sha=_SHA)
        assert verify_checkout_sha(checkout, _SHA2) is False

    def test_returns_false_when_checkout_dir_missing(self, tmp_path):
        assert verify_checkout_sha(tmp_path / "missing", _SHA) is False

    def test_returns_false_when_head_unreadable(self, tmp_path):
        # Directory exists but has no .git at all
        assert verify_checkout_sha(tmp_path, _SHA) is False

    def test_accepts_uppercase_expected_sha(self, tmp_path):
        checkout = _make_git_dir(tmp_path, sha=_SHA)
        assert verify_checkout_sha(checkout, _SHA.upper()) is True

    def test_accepts_sha_with_trailing_whitespace(self, tmp_path):
        checkout = _make_git_dir(tmp_path, sha=_SHA)
        assert verify_checkout_sha(checkout, f"  {_SHA}  ") is True

    def test_returns_true_via_symbolic_ref(self, tmp_path):
        checkout = _make_git_dir_with_ref(tmp_path, sha=_SHA)
        assert verify_checkout_sha(checkout, _SHA) is True
