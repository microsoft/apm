"""Verdict keys resolve URL rewrites from one config probe per run."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-ref-freshness")]


def test_url_rewrites_are_probed_once_for_many_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keying verdicts for many origins costs one git config probe, not one per origin."""
    from apm_cli.cache.commit_reachability import effective_contact

    probes: list[object] = []

    def _rewrites(env):
        probes.append(env)
        return (("https://mirror.example/", "https://github.com/rewritten/"),)

    monkeypatch.setattr("apm_cli.utils.git_env.git_url_rewrites", _rewrites)
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}

    plain = [effective_contact(f"https://github.com/o/pkg-{i}", env) for i in range(5)]
    rewritten = effective_contact("https://github.com/rewritten/pkg", env)

    assert len(probes) == 1
    assert all(contact is not None and "/o/pkg-" in contact for contact in plain)
    assert rewritten is not None
    assert urllib.parse.urlsplit(rewritten).hostname == "mirror.example"


def test_a_ref_tip_of_the_fresh_clone_needs_no_object_probe(tmp_path: Path) -> None:
    from apm_cli.cache.git_cache import _is_ref_tip

    packed, loose, other = "a" * 40, "b" * 40, "c" * 40
    git_dir = tmp_path / "staged.git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{packed} refs/heads/main\n",
        encoding="ascii",
    )
    (git_dir / "refs" / "heads" / "dev").write_text(loose + "\n", encoding="ascii")

    assert _is_ref_tip(git_dir, packed.upper())
    assert _is_ref_tip(git_dir, loose)
    assert not _is_ref_tip(git_dir, other)
    assert not _is_ref_tip(tmp_path / "missing.git", packed)


_A, _B, _T = "a" * 40, "b" * 40, "d" * 40
_HEADER = "# pack-refs with: peeled fully-peeled sorted\n"


def _clone_dir(tmp_path: Path, packed: str | None, loose: dict[str, str]) -> Path:
    git_dir = tmp_path / "staged.git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    if packed is not None:
        (git_dir / "packed-refs").write_text(packed, encoding="ascii")
    for name, text in loose.items():
        path = git_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    return git_dir


@pytest.mark.parametrize(
    ("packed", "loose", "sha", "named"),
    [
        pytest.param(f"{_HEADER}{_A} refs/heads/main\n", {}, _A, True, id="packed-branch"),
        pytest.param(None, {"refs/heads/dev": f"{_B}\n"}, _B, True, id="loose-branch"),
        # A loose ref wins over its packed entry, as in git: the stale packed value is no tip.
        pytest.param(
            f"{_HEADER}{_A} refs/heads/main\n",
            {"refs/heads/main": f"{_B}\n"},
            _A,
            False,
            id="packed-loose-conflict-stale",
        ),
        pytest.param(
            f"{_HEADER}{_A} refs/heads/main\n",
            {"refs/heads/main": f"{_B}\n"},
            _B,
            True,
            id="packed-loose-conflict-current",
        ),
        # An annotated tag names a tag object, not a commit; its peel line is no ref.
        pytest.param(f"{_HEADER}{_T} refs/tags/v1\n^{_A}\n", {}, _T, False, id="peeled-tag-object"),
        pytest.param(f"{_HEADER}{_T} refs/tags/v1\n^{_A}\n", {}, _A, False, id="peeled-commit"),
        pytest.param(None, {"refs/tags/v1": f"{_T}\n"}, _T, False, id="loose-tag"),
        # A branch that peels (names a tag object) is unusual: ask git.
        pytest.param(f"{_HEADER}{_T} refs/heads/odd\n^{_A}\n", {}, _T, False, id="peeled-branch"),
        # A symbolic ref names no commit itself and hides its packed entry.
        pytest.param(
            None,
            {"refs/heads/alias": "ref: refs/tags/v1\n", "refs/tags/v1": f"{_T}\n"},
            _T,
            False,
            id="symref-to-tag",
        ),
        pytest.param(
            f"{_HEADER}{_A} refs/heads/alias\n",
            {"refs/heads/alias": "ref: refs/heads/gone\n"},
            _A,
            False,
            id="symref-hides-packed",
        ),
        # Malformed packed-refs: any bad line means git is asked, even after a match.
        pytest.param(
            f"{_HEADER}{_A} refs/heads/main\ngarbage\n", {}, _A, False, id="malformed-after-match"
        ),
        pytest.param(
            f"{_HEADER}garbage\n{_A} refs/heads/main\n", {}, _A, False, id="malformed-before-match"
        ),
        pytest.param(f"{_HEADER}{_A}\n", {}, _A, False, id="malformed-no-refname"),
        pytest.param(
            f"{_HEADER}{_A[:39]} refs/heads/main\n{_A} refs/heads/x\n",
            {},
            _A,
            False,
            id="malformed-short-oid",
        ),
        pytest.param(f"^{_A}\n{_A} refs/heads/main\n", {}, _A, False, id="malformed-orphan-peel"),
        pytest.param(
            f"{_HEADER}{_A} refs/heads/main\n\n", {}, _A, False, id="malformed-blank-line"
        ),
        pytest.param(
            f"{_HEADER}{_A} refs/heads/main\n",
            {"refs/heads/main.lock": f"{_B}\n"},
            _A,
            False,
            id="ref-update-in-flight",
        ),
        pytest.param(
            f"{_HEADER}{_A} refs/heads/main\n",
            {"refs/heads/dev": "not-an-oid\n"},
            _A,
            False,
            id="malformed-loose",
        ),
    ],
)
def test_a_ref_tip_is_read_as_git_reads_it_or_git_is_asked(
    tmp_path: Path, packed: str | None, loose: dict[str, str], sha: str, named: bool
) -> None:
    """Only a branch tip git itself resolves skips the probe; anything unusual asks git."""
    from apm_cli.cache.git_cache import _is_ref_tip

    assert _is_ref_tip(_clone_dir(tmp_path, packed, loose), sha) is named


def test_a_non_ascii_or_reftable_clone_asks_git(tmp_path: Path) -> None:
    from apm_cli.cache.git_cache import _is_ref_tip

    git_dir = _clone_dir(tmp_path, None, {})
    (git_dir / "packed-refs").write_bytes(
        f"{_HEADER}{_A} refs/heads/main\n".encode() + b"\xff refs/heads/x\n"
    )
    assert _is_ref_tip(git_dir, _A) is False

    reftable = _clone_dir(tmp_path / "rt", f"{_HEADER}{_A} refs/heads/main\n", {})
    (reftable / "reftable").mkdir()
    assert _is_ref_tip(reftable, _A) is False


class _FlakyResolver:
    """A tiered, read-only-cache downloader whose default branch fails *failures* times."""

    cache_read_only = True  # the real tip recorder runs and writes nothing

    def __init__(self, failures: int, sha: str) -> None:
        from types import SimpleNamespace

        from apm_cli.models.dependency.types import GitReferenceType

        self.failures = failures
        self.calls = 0
        self._resolved = SimpleNamespace(resolved_commit=sha, ref_type=GitReferenceType.BRANCH)
        self._tiered_resolver = SimpleNamespace(remotely_resolved=lambda _dep, _sha: True)

    def resolve_git_reference(self, _dep_ref):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient resolver failure")
        return self._resolved

    def _build_repo_url(self, url: str, *, use_ssh: bool, dep_ref) -> str:
        return f"https://github.com/{url}"

    def _persistent_cache_remote(self, _dep_ref, url: str) -> str:
        return url


def test_a_default_branch_retry_is_fetched_by_its_admitted_commit() -> None:
    """#5199: a retry's admitted commit is fetched, never the branch by name."""
    from apm_cli.cache.commit_reachability import admit_fetch_ref
    from apm_cli.models.dependency.reference import DependencyReference

    sha = "a" * 40
    downloader = _FlakyResolver(1, sha)

    fetched = admit_fetch_ref(downloader, DependencyReference.parse("o/pkg"), None)

    assert fetched == sha
    assert downloader.calls == 2


def test_a_default_branch_that_never_resolves_is_refused() -> None:
    """#5199: with the tiered resolver, an unresolvable default branch is never fetched by name."""
    from apm_cli.cache.commit_reachability import CommitNotReachableError, admit_fetch_ref
    from apm_cli.models.dependency.reference import DependencyReference

    with pytest.raises(CommitNotReachableError, match="default branch"):
        admit_fetch_ref(_FlakyResolver(2, "a" * 40), DependencyReference.parse("o/pkg"), None)
