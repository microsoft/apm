"""Stub the checkout content verifier for tests whose checkouts are fakes.

Tests that lay down a bare ``.git`` directory (or mock the clone) to drive
cache hit, miss and eviction mechanics have no real objects to verify. The
stub accepts whatever bytes the checkout holds and records them, so the
consumers' copy and read checks still compare against what was "verified".
The verifier itself is covered against real repositories in
``tests/unit/cache/test_checkout_verify.py``.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import patch


def _accept_worktree(repo, sha, _scope, _cone, _git_exe, _env):
    from apm_cli.cache import checkout_verify
    from apm_cli.utils.git_object_id import git_object_id, object_format

    try:
        fmt = object_format(sha)
    except ValueError:
        fmt = "sha1"  # an abbreviated id: git's default object format
    files = {}
    for rel, path in checkout_verify._worktree_files(repo):
        executable = os.name != "nt" and bool(os.lstat(path).st_mode & stat.S_IXUSR)
        files[rel] = (git_object_id(fmt, "blob", path.read_bytes()), executable)
    return checkout_verify._Manifest(fmt, files)


def trust_checkout_contents():
    """Return a patcher (decorator or context manager) that accepts every checkout as is."""
    return patch("apm_cli.cache.checkout_verify._verify", new=_accept_worktree)
