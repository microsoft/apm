"""Pure helper functions for parsing and sorting git remote references.

These are stateless utilities extracted from GitHubPackageDownloader to
improve module cohesion.  They accept data in and return data out with
no side effects.
"""

import re
from typing import Dict, List  # noqa: F401, UP035

from ..models.apm_package import GitReferenceType, RemoteRef


def parse_ls_remote_output(output: str) -> list[RemoteRef]:
    """Parse ``git ls-remote --tags --heads`` output into RemoteRef objects.

    Format per line: ``<sha>\\t<refname>``

    For annotated tags git emits two lines::

        <tag-object-sha>   refs/tags/v1.0.0
        <commit-sha>       refs/tags/v1.0.0^{}

    We want the commit SHA (from the ``^{}`` line) and skip the
    tag-object-only line.

    Args:
        output: Raw stdout from ``git ls-remote``.

    Returns:
        Unsorted list of RemoteRef.
    """
    tags: dict[str, str] = {}  # tag name -> commit sha
    branches: list[RemoteRef] = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        sha, refname = parts[0].strip(), parts[1].strip()

        if refname.startswith("refs/tags/"):
            tag_name = refname[len("refs/tags/") :]
            if tag_name.endswith("^{}"):
                # Dereferenced commit -- overwrite with the real commit SHA
                tag_name = tag_name[:-3]
                tags[tag_name] = sha
            else:
                # Only store if we haven't seen the deref line yet
                tags.setdefault(tag_name, sha)

        elif refname.startswith("refs/heads/"):
            branch_name = refname[len("refs/heads/") :]
            branches.append(
                RemoteRef(
                    name=branch_name,
                    ref_type=GitReferenceType.BRANCH,
                    commit_sha=sha,
                )
            )

    tag_refs = [
        RemoteRef(name=name, ref_type=GitReferenceType.TAG, commit_sha=sha)
        for name, sha in tags.items()
    ]
    return tag_refs + branches


def semver_sort_key(name: str):
    """Return a sort key for semver-like tag names (descending).

    Non-semver tags sort after all semver tags, alphabetically.
    """
    clean = name.lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)", clean)
    if m:
        # Negate for descending order within the first group
        return (0, -int(m.group(1)), -int(m.group(2)), -int(m.group(3)), m.group(4))
    return (1, name)


def sort_remote_refs(refs: list[RemoteRef]) -> list[RemoteRef]:
    """Sort refs: tags first (semver descending), then branches alphabetically."""
    tags = [r for r in refs if r.ref_type == GitReferenceType.TAG]
    branches = [r for r in refs if r.ref_type == GitReferenceType.BRANCH]
    tags.sort(key=lambda r: semver_sort_key(r.name))
    branches.sort(key=lambda r: r.name)
    return tags + branches
