"""Dependency type definitions  -- enums and simple dataclasses."""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional  # noqa: F401


class GitReferenceType(Enum):
    """Types of Git references supported."""

    BRANCH = "branch"
    TAG = "tag"
    COMMIT = "commit"


@dataclass
class RemoteRef:
    """A single remote Git reference (tag or branch) with its commit SHA."""

    name: str
    ref_type: GitReferenceType
    commit_sha: str


class VirtualPackageType(Enum):
    """Type of virtual package."""

    FILE = "file"  # Individual file (*.prompt.md, etc.)
    COLLECTION = "collection"  # Collection virtual package
    SUBDIRECTORY = "subdirectory"  # Subdirectory package


@dataclass
class ResolvedReference:
    """Represents a resolved Git reference."""

    original_ref: str
    ref_type: GitReferenceType
    resolved_commit: str | None = None
    ref_name: str = ""  # The actual branch/tag/commit name

    def __str__(self) -> str:
        """String representation of resolved reference."""
        if not self.resolved_commit:
            return self.ref_name
        if self.ref_type == GitReferenceType.COMMIT:
            return f"{self.resolved_commit[:8]}"
        return f"{self.ref_name} ({self.resolved_commit[:8]})"


def parse_git_reference(ref_string: str) -> tuple[GitReferenceType, str]:
    """Parse a git reference string to determine its type.

    Args:
        ref_string: Git reference (branch, tag, or commit)

    Returns:
        tuple: (GitReferenceType, cleaned_reference)
    """
    if not ref_string:
        return GitReferenceType.BRANCH, "main"  # Default to main branch

    ref = ref_string.strip()

    # Check if it looks like a commit SHA (40 hex chars or 7+ hex chars)
    if re.match(r"^[a-f0-9]{7,40}$", ref.lower()):
        return GitReferenceType.COMMIT, ref

    # Check if it looks like a semantic version tag
    if re.match(r"^v?\d+\.\d+\.\d+", ref):
        return GitReferenceType.TAG, ref

    # Otherwise assume it's a branch
    return GitReferenceType.BRANCH, ref
