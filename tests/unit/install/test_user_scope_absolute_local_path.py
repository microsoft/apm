"""Absolute local paths accepted in user and project scope."""

from __future__ import annotations

import os

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.package_resolution import (
    user_scope_rejection_reason,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component


def _local_ref(path: str) -> DependencyReference:
    """Build a minimal local DependencyReference."""
    return DependencyReference(
        repo_url="local",
        is_local=True,
        local_path=path,
    )


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------


def test_project_scope_accepts_absolute_local_path(tmp_path):
    """Symmetry check: project scope is also fine with absolute local paths."""
    assert (
        user_scope_rejection_reason(_local_ref(str(tmp_path)), scope=InstallScope.PROJECT) is None
    )


# ---------------------------------------------------------------------------
# User-scope local-path policy (the regression hot-spot)
# ---------------------------------------------------------------------------


def test_user_scope_accepts_absolute_local_path(tmp_path):
    """Absolute local paths are unambiguous -- post-#937 contract.

    This is the assertion that catches the #1149 regression: an
    unconditional ``dep_ref.is_local`` check would fail this test.
    """
    abs_path = str(tmp_path / "local-pkg")
    assert os.path.isabs(abs_path), "test setup invariant: tmp_path is absolute"
    assert user_scope_rejection_reason(_local_ref(abs_path), scope=InstallScope.USER) is None


# ---------------------------------------------------------------------------
# User-scope remote / parent-inheritance handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Error wording sanity
# ---------------------------------------------------------------------------
