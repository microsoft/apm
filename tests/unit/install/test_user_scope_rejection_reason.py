"""Regression-trap tests for ``user_scope_rejection_reason``.

This module pins the post-#937 / post-#1149 contract:

  * Project scope            -> NEVER reject (manifest gates ambiguity).
  * User scope, remote ref   -> NEVER reject (the happy path).
  * User scope, local *abs*  -> NEVER reject (an absolute path is unambiguous;
                                see PR #937 commit message).
  * User scope, local *rel*  -> ALWAYS reject (relative-to-cwd is ambiguous
                                outside a project; ``$HOME`` is not a project
                                root).
  * User scope, ``git: parent`` inheritance -> ALWAYS reject (no monorepo
                                root at user scope).

A regression here previously shipped to release because the only coverage
was a single slow E2E (``test_auto_bootstrap_creates_user_manifest``) that
was not wired into CI -- the #1149 GitLab refactor regressed the absolute-
path branch and went undetected for an entire release window.

Keeping these as fast unit tests means the next refactor that touches the
predicate fails one assertion in <10ms instead of one slow E2E in 14s.
"""

from __future__ import annotations

import os

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.package_resolution import (
    GIT_PARENT_USER_SCOPE_ERROR,
    user_scope_rejection_reason,
)
from apm_cli.models.dependency.reference import DependencyReference


def _local_ref(path: str) -> DependencyReference:
    """Build a minimal local DependencyReference."""
    return DependencyReference(
        repo_url="local",
        is_local=True,
        local_path=path,
    )


def _remote_ref() -> DependencyReference:
    return DependencyReference(repo_url="acme/widgets")


def _parent_ref() -> DependencyReference:
    return DependencyReference(
        repo_url="acme/monorepo",
        is_parent_repo_inheritance=True,
        virtual_path="packages/widgets",
    )


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------


def test_scope_none_never_rejects():
    """A None scope short-circuits to None (no scope, no policy)."""
    assert user_scope_rejection_reason(_local_ref("./pkg"), scope=None) is None
    assert user_scope_rejection_reason(_remote_ref(), scope=None) is None


def test_project_scope_accepts_relative_local_path():
    """Project scope must never reject local paths (only USER scope is policed)."""
    assert user_scope_rejection_reason(_local_ref("./pkg"), scope=InstallScope.PROJECT) is None


def test_project_scope_accepts_absolute_local_path(tmp_path):
    """Symmetry check: project scope is also fine with absolute local paths."""
    assert (
        user_scope_rejection_reason(_local_ref(str(tmp_path)), scope=InstallScope.PROJECT) is None
    )


def test_project_scope_accepts_parent_inheritance():
    """``git: parent`` is a project-scope construct; project scope must accept it."""
    assert user_scope_rejection_reason(_parent_ref(), scope=InstallScope.PROJECT) is None


# ---------------------------------------------------------------------------
# User-scope local-path policy (the regression hot-spot)
# ---------------------------------------------------------------------------


def test_user_scope_rejects_relative_local_path():
    """Relative local paths are ambiguous at user scope -- must be rejected."""
    reason = user_scope_rejection_reason(_local_ref("./pkg"), scope=InstallScope.USER)
    assert reason is not None
    assert "relative" in reason.lower()
    assert "user scope" in reason or "--global" in reason


def test_user_scope_rejects_dotted_relative_local_path():
    """``../sibling`` is also relative -- must be rejected at user scope."""
    reason = user_scope_rejection_reason(_local_ref("../sibling-pkg"), scope=InstallScope.USER)
    assert reason is not None
    assert "relative" in reason.lower()


def test_user_scope_rejects_bare_directory_local_path():
    """A bare directory name (no ``./``) is still relative -- must be rejected."""
    reason = user_scope_rejection_reason(_local_ref("local-pkg"), scope=InstallScope.USER)
    assert reason is not None
    assert "relative" in reason.lower()


def test_user_scope_accepts_absolute_local_path(tmp_path):
    """Absolute local paths are unambiguous -- post-#937 contract.

    This is the assertion that catches the #1149 regression: an
    unconditional ``dep_ref.is_local`` check would fail this test.
    """
    abs_path = str(tmp_path / "local-pkg")
    assert os.path.isabs(abs_path), "test setup invariant: tmp_path is absolute"
    assert user_scope_rejection_reason(_local_ref(abs_path), scope=InstallScope.USER) is None


@pytest.mark.parametrize("tilde_path", ["~/pkg", "~/sub/pkg"])
def test_user_scope_accepts_tilde_local_path(tilde_path):
    """`~/pkg` is absolute after expanduser() and must NOT be rejected.

    The rest of the install pipeline (sources.py, phases/resolve.py)
    expanduser()s local paths before consuming them, so a `~`-prefixed
    path reaches `_copy_local_package` as an absolute path. The
    rejection predicate must mirror that contract -- without it, every
    `apm install --global ~/pkg` invocation is incorrectly rejected.
    """
    assert user_scope_rejection_reason(_local_ref(tilde_path), scope=InstallScope.USER) is None


def test_user_scope_handles_empty_local_path_defensively():
    """A malformed ref with empty local_path is treated as relative (rejected).

    ``Path("").is_absolute()`` is False, so the predicate must classify
    an empty path as relative -- *not* crash and *not* silently accept.
    """
    ref = DependencyReference(repo_url="local", is_local=True, local_path=None)
    reason = user_scope_rejection_reason(ref, scope=InstallScope.USER)
    assert reason is not None
    assert "relative" in reason.lower()


# ---------------------------------------------------------------------------
# User-scope remote / parent-inheritance handling
# ---------------------------------------------------------------------------


def test_user_scope_accepts_remote_reference():
    """Remote references are the canonical user-scope happy path."""
    assert user_scope_rejection_reason(_remote_ref(), scope=InstallScope.USER) is None


def test_user_scope_rejects_parent_repo_inheritance():
    """``git: parent`` cannot resolve at user scope -- there is no parent repo."""
    reason = user_scope_rejection_reason(_parent_ref(), scope=InstallScope.USER)
    assert reason == GIT_PARENT_USER_SCOPE_ERROR


# ---------------------------------------------------------------------------
# Error wording sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", ["./pkg", "../sibling", "pkg", "sub/dir/pkg"])
def test_user_scope_relative_rejection_mentions_recovery(rel_path):
    """Every relative-rejection message must steer the user toward a fix."""
    reason = user_scope_rejection_reason(_local_ref(rel_path), scope=InstallScope.USER)
    assert reason is not None
    lowered = reason.lower()
    # Should mention either "absolute" or "remote reference" so the user
    # knows what to type next instead of just being told "no".
    assert "absolute" in lowered or "remote reference" in lowered
