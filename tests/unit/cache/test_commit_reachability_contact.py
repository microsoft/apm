"""Verdict keys resolve URL rewrites from one config probe per run."""

from __future__ import annotations

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
    monkeypatch.setattr("apm_cli.cache.commit_reachability._REWRITES", {})
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}

    plain = [effective_contact(f"https://github.com/o/pkg-{i}", env) for i in range(5)]
    rewritten = effective_contact("https://github.com/rewritten/pkg", env)

    assert len(probes) == 1
    assert all(contact is not None and "/o/pkg-" in contact for contact in plain)
    assert rewritten is not None and rewritten.startswith("https://mirror.example")


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


def test_ref_tips_agree_with_git_on_a_real_no_tags_clone(tmp_path: Path) -> None:
    """A real --no-tags clone with packed refs and a newer loose branch tip, checked against git."""
    import shutil
    import subprocess

    from apm_cli.cache.git_cache import _is_ref_tip

    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def run(*args: str) -> str:
        return subprocess.run(
            [git, *args], check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    src = tmp_path / "src"
    run("init", "-q", "-b", "main", str(src))
    run("-C", str(src), "commit", "-q", "--allow-empty", "-m", "one")
    first = run("-C", str(src), "rev-parse", "HEAD")
    run("-C", str(src), "tag", "-a", "v1", "-m", "v1")
    tag_object = run("-C", str(src), "rev-parse", "v1")
    run("-C", str(src), "commit", "-q", "--allow-empty", "-m", "two")
    second = run("-C", str(src), "rev-parse", "HEAD")

    clone = tmp_path / "staged.git"
    run("clone", "-q", "--bare", "--no-tags", str(src), str(clone))
    gd = ("--git-dir", str(clone))
    run(*gd, "fetch", "-q", str(src), "refs/tags/v1:refs/tags/v1")
    run(*gd, "pack-refs", "--all")
    run(*gd, "update-ref", "refs/heads/main", first)  # loose main now overrides packed main

    assert run(*gd, "rev-parse", "refs/heads/main") == first
    assert (clone / "refs" / "heads" / "main").is_file()
    assert second in (clone / "packed-refs").read_text(encoding="ascii")
    assert run(*gd, "cat-file", "-t", tag_object) == "tag"

    assert _is_ref_tip(clone, first) is True
    assert _is_ref_tip(clone, second) is False
    assert _is_ref_tip(clone, tag_object) is False
