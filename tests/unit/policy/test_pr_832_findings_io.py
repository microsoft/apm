"""Tests for #832 PR-review findings: cross-cutting policy hardening (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import os

import pytest

from apm_cli.policy.discovery import (
    _get_cache_dir,
)

pytestmark = pytest.mark.component

# ──────────────────────────────────────────────────────────────────────
# #2: PolicyBlockError is an alias of PolicyViolationError
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# #3: pipeline does not double-wrap PolicyViolationError
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# #4: outcome routing table -- single source of truth smoke
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# #5: dry-run falls back to check.name when details is empty
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# #6: dep-ref parsing contract + defensive fallback
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# #7: cache path containment
# ──────────────────────────────────────────────────────────────────────


class TestCachePathContainment:
    def test_normal_layout_returns_user_owned_cache_path(self, tmp_path):
        cache_dir = _get_cache_dir(tmp_path)
        assert cache_dir.parent.name == "policy_v1"
        assert not cache_dir.is_relative_to(tmp_path)

    def test_symlinked_apm_modules_does_not_affect_policy_cache(self, tmp_path):
        project = tmp_path / "project"
        evil = tmp_path / "elsewhere"
        project.mkdir()
        evil.mkdir()
        symlink = project / "apm_modules"
        try:
            os.symlink(evil, symlink)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform")

        cache_dir = _get_cache_dir(project)
        assert cache_dir.parent.name == "policy_v1"
        assert not cache_dir.is_relative_to(evil)

    def test_unresolved_project_root_does_not_raise(self, tmp_path):
        # Regression for #886: on Windows, tempfile.mkdtemp() may return
        # an 8.3 short-name path (e.g. RUNNER~1). _get_cache_dir must
        # resolve project_root before building the candidate path so
        # both sides of ensure_path_within use consistent long names.
        real = tmp_path / "real-project"
        real.mkdir()
        try:
            link = tmp_path / "indirect"
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported on this platform")

        cache_dir = _get_cache_dir(link)
        assert cache_dir == _get_cache_dir(real)


# ──────────────────────────────────────────────────────────────────────
# #8: discover_policy_with_chain has no ``no_policy`` parameter
# ──────────────────────────────────────────────────────────────────────
