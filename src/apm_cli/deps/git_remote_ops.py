"""Pure helper functions for parsing and sorting git remote references.

These are stateless utilities extracted from GitHubPackageDownloader to
improve module cohesion.  They accept data in and return data out with
no side effects.
"""

import re

from ..models.apm_package import GitReferenceType, RemoteRef


class RemoteRefParseError(RuntimeError):
    """Raised when git ls-remote output cannot be safely interpreted."""


_REMOTE_SHA_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_INVALID_REF_CHARS = frozenset(" ~^:?*[\\")


def _is_valid_tag_refname(refname: str) -> bool:
    """Return whether *refname* follows Git's tag refname restrictions."""
    if not refname.startswith("refs/tags/"):
        return False
    tag_name = refname.removeprefix("refs/tags/")
    if not tag_name or tag_name == "@" or tag_name.endswith("."):
        return False
    if ".." in tag_name or "@{" in tag_name or "//" in tag_name:
        return False
    if any(ord(char) < 32 or ord(char) == 127 or char in _INVALID_REF_CHARS for char in tag_name):
        return False
    components = tag_name.split("/")
    return all(
        component and not component.startswith(".") and not component.casefold().endswith(".lock")
        for component in components
    )


def validate_ls_remote_tag_output(output: str) -> None:
    """Reject malformed output from ``git ls-remote --tags``.

    An empty response is valid for a repository without tags. Any nonempty
    response must use the documented SHA-and-tag-ref wire format; otherwise a
    revision-pin refresh must fail rather than treating transport corruption as
    a missing release tag.
    """
    if output == "":
        return

    plain_tags: set[str] = set()
    peeled_tags: set[str] = set()
    records: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            raise RemoteRefParseError("Malformed git ls-remote tag output.")
        parts = line.split("\t")
        if len(parts) != 2:
            raise RemoteRefParseError("Malformed git ls-remote tag output.")
        raw_sha, raw_refname = parts
        if raw_sha != raw_sha.strip() or raw_refname != raw_refname.strip():
            raise RemoteRefParseError("Malformed git ls-remote tag output.")
        sha, refname = raw_sha, raw_refname
        if not _REMOTE_SHA_RE.fullmatch(sha) or sha == "0" * 40 or refname in records:
            raise RemoteRefParseError("Malformed git ls-remote tag output.")
        records[refname] = sha
        is_peeled = refname.endswith("^{}")
        base_refname = refname[:-3] if is_peeled else refname
        if not _is_valid_tag_refname(base_refname):
            raise RemoteRefParseError("Malformed git ls-remote tag output.")
        tag_name = base_refname.removeprefix("refs/tags/")
        if is_peeled:
            peeled_tags.add(tag_name)
        else:
            plain_tags.add(tag_name)
    if peeled_tags - plain_tags:
        raise RemoteRefParseError("Malformed git ls-remote tag output.")


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
    annotated_tags: set[str] = set()
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
                # Dereferenced commit -- overwrite with the real commit SHA.
                #
                # SECURITY INVARIANT (load-bearing, do not weaken): only
                # ANNOTATED tags emit this peeled ``^{}`` line, so the
                # presence of a peeled ref is our sole signal for
                # ``annotated=True``. The revision-pin resolver
                # (find_latest_annotated_tag) accepts ONLY annotated tags and
                # rejects branches and lightweight tags fail-closed, so a
                # branch or lightweight tag named like a release can never
                # masquerade as a SHA-pin update target. A transport that
                # suppressed peeled refs would misclassify a genuine annotated
                # tag as lightweight. Revision-pin updates then retain the
                # current SHA rather than selecting an unverified target, which
                # is the safe direction. Any future edit here that marks a
                # non-peeled ref as annotated would break this anti-spoofing
                # fence.
                tag_name = tag_name[:-3]
                tags[tag_name] = sha
                annotated_tags.add(tag_name)
            else:
                # Only store if we haven't seen the deref line yet.
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
        RemoteRef(
            name=name,
            ref_type=GitReferenceType.TAG,
            commit_sha=sha,
            annotated=name in annotated_tags,
        )
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
