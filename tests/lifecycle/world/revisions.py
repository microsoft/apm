"""New git revisions of world repositories, undone on reset (#4094).

``publish_revision`` commits file changes on ``main`` of a served bare
repository and can create or move a tag, so a test can simulate an upstream
that changed under a ref (for example a native plugin whose definition changed
at the same tag). World services are shared per pytest worker, so the first
change snapshots every ref and ``restore_refs`` puts them back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "APM Lifecycle",
    "GIT_AUTHOR_EMAIL": "lifecycle@example.invalid",
    "GIT_COMMITTER_NAME": "APM Lifecycle",
    "GIT_COMMITTER_EMAIL": "lifecycle@example.invalid",
}


def _git(repository: Path, *args: str, env: Mapping[str, str] | None = None, data: bytes = b""):
    git = shutil.which("git") or "git"
    result = subprocess.run(
        (git, "--git-dir", str(repository), "-c", "commit.gpgsign=false", *args),
        input=data,
        capture_output=True,
        env={**os.environ, **_IDENTITY, **(env or {})},
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout.decode("utf-8").strip()


def snapshot_refs(repository: Path) -> dict[str, str]:
    listing = _git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in listing.splitlines() if line)


def restore_refs(repository: Path, refs: Mapping[str, str]) -> None:
    for name in set(snapshot_refs(repository)) - set(refs):
        _git(repository, "update-ref", "-d", name)
    for name, sha in refs.items():
        _git(repository, "update-ref", name, sha)


def publish_revision(
    repository: Path,
    files: Mapping[str, str | bytes | None],
    *,
    message: str,
    tag: str | None = None,
) -> str:
    """Commit *files* (relative path -> content, ``None`` deletes) on main; return the sha."""
    parent = _git(repository, "rev-parse", "refs/heads/main")
    with tempfile.TemporaryDirectory(prefix="lifecycle-index-") as scratch:
        index = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repository, "read-tree", parent, env=index)
        for path, content in files.items():
            if content is None:
                _git(repository, "update-index", "--force-remove", path, env=index)
                continue
            data = content.encode("utf-8") if isinstance(content, str) else content
            blob = _git(repository, "hash-object", "-w", "--stdin", data=data)
            _git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{path}",
                env=index,
            )
        tree = _git(repository, "write-tree", env=index)
    commit = _git(repository, "commit-tree", tree, "-p", parent, "-m", message)
    _git(repository, "update-ref", "refs/heads/main", commit, parent)
    if tag is not None:
        _git(repository, "update-ref", f"refs/tags/{tag}", commit)
    return commit
