"""Marketplace ``source.sha`` for annotated, signed and lightweight tags (#3048).

Plugin installers check out the selected tag and compare the result with
``source.sha``. A checkout of an annotated or signed tag lands on the commit
the tag points to, so that commit, not the tag object, has to be recorded.
Git creates the tags in a local bare repository; the tests list them with
``git ls-remote`` and pack them into a marketplace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.ref_resolver import RemoteRef, _parse_ls_remote_output
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

_SOURCE = "acme/tagged-package"


@dataclass(frozen=True)
class _TaggedRepository:
    env: dict[str, str]
    repository: LocalGitRepository
    commits: dict[str, str]  # tag name -> commit the tag points to


def _git(env: dict[str, str], cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _remote_refs(tagged: _TaggedRepository) -> list[RemoteRef]:
    output = _git(
        tagged.env,
        tagged.repository.worktree,
        "ls-remote",
        "--tags",
        "--heads",
        tagged.repository.file_url,
    )
    return _parse_ls_remote_output(output)


@pytest.fixture
def tagged(tmp_path: Path) -> _TaggedRepository:
    """v1.0.0 is annotated, v1.1.0 lightweight, each on its own commit."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "fixture", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("tagged-package")
    readme = repository.worktree / "README.md"
    readme.write_text("first\n", encoding="utf-8")
    first = repositories.commit(repository, message="first")
    repositories.tag(repository, "v1.0.0", first, annotated=True)
    readme.write_text("second\n", encoding="utf-8")
    second = repositories.commit(repository, message="second")
    repositories.tag(repository, "v1.1.0", second)
    return _TaggedRepository(
        env=env,
        repository=repository,
        commits={"v1.0.0": first.sha, "v1.1.0": second.sha},
    )


def test_tags_resolve_to_the_commit_a_checkout_lands_on(tagged: _TaggedRepository) -> None:
    worktree = tagged.repository.worktree
    tag_object = _git(tagged.env, worktree, "rev-parse", "v1.0.0")
    assert tag_object != tagged.commits["v1.0.0"]  # annotated: its object is not the commit

    shas = {ref.name: ref.sha for ref in _remote_refs(tagged)}

    assert shas["refs/tags/v1.0.0"] == tagged.commits["v1.0.0"]
    assert shas["refs/tags/v1.1.0"] == tagged.commits["v1.1.0"]
    assert shas["refs/heads/main"] == tagged.commits["v1.1.0"]
    assert not any(name.endswith("^{}") for name in shas)


def test_ssh_signed_tag_resolves_to_its_commit(tagged: _TaggedRepository, tmp_path: Path) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is needed to sign a tag")
    key = tmp_path / "signing-key"
    subprocess.run(
        ("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "apm-test", "-f", str(key)),
        capture_output=True,
        check=True,
        timeout=30,
    )
    worktree = tagged.repository.worktree
    _git(
        tagged.env,
        worktree,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={key}",
        "tag",
        "-s",
        "v2.0.0",
        tagged.commits["v1.1.0"],
        "-m",
        "Release v2.0.0",
    )
    _git(tagged.env, worktree, "push", "origin", "refs/tags/v2.0.0")
    assert "BEGIN SSH SIGNATURE" in _git(tagged.env, worktree, "cat-file", "tag", "v2.0.0")

    shas = {ref.name: ref.sha for ref in _remote_refs(tagged)}

    assert shas["refs/tags/v2.0.0"] == tagged.commits["v1.1.0"]


class _ResolverFromRefs:
    """Stand-in for RefResolver that serves refs already read from git."""

    def __init__(self, refs: list[RemoteRef]) -> None:
        self._refs = refs

    def list_remote_refs(
        self, owner_repo: str, *, remote_url: str | None = None
    ) -> list[RemoteRef]:
        assert owner_repo == _SOURCE
        return self._refs

    def close(self) -> None:
        pass


def test_pack_records_the_commit_a_checkout_of_the_ref_lands_on(
    tagged: _TaggedRepository, tmp_path: Path
) -> None:
    yml = tmp_path / "marketplace.yml"
    yml.write_text(
        textwrap.dedent(
            f"""\
            name: tag-marketplace
            description: Annotated tag sources
            version: 1.0.0
            owner:
              name: Test
            packages:
              - name: annotated-ref
                source: {_SOURCE}
                ref: v1.0.0
              - name: annotated-range
                source: {_SOURCE}
                version: "~1.0.0"
              - name: lightweight-ref
                source: {_SOURCE}
                ref: v1.1.0
            """
        ),
        encoding="utf-8",
    )
    builder = MarketplaceBuilder(yml, BuildOptions(offline=True))
    builder._resolver = _ResolverFromRefs(_remote_refs(tagged))  # type: ignore[assignment]
    report = builder.build()

    plugins = json.loads(report.output_path.read_text("utf-8"))["plugins"]
    sources = {plugin["name"]: plugin["source"] for plugin in plugins}
    assert {name: (source["ref"], source["sha"]) for name, source in sources.items()} == {
        "annotated-ref": ("v1.0.0", tagged.commits["v1.0.0"]),
        "annotated-range": ("v1.0.0", tagged.commits["v1.0.0"]),
        "lightweight-ref": ("v1.1.0", tagged.commits["v1.1.0"]),
    }

    # What an installer does with the entry: clone, check out the ref, compare.
    clone = tmp_path / "clone"
    _git(tagged.env, tmp_path, "clone", "--quiet", tagged.repository.file_url, str(clone))
    for source in sources.values():
        _git(tagged.env, clone, "checkout", "--quiet", "--detach", source["ref"])
        assert _git(tagged.env, clone, "rev-parse", "HEAD") == source["sha"]
