"""Unit tests for apm_cli.security.file_scanner (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.security.file_scanner import (
    _minimal_governed_prefixes,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _is_safe_lockfile_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _scan_files_in_dir
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# scan_lockfile_packages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# scan_deployed_trees
# ---------------------------------------------------------------------------

# Escaped, not literal: this suite's existing Unicode tests write payloads as
# escapes (see tests/unit/test_content_scanner.py), and a raw bidi override in
# source is invisible in review -- the exact hazard under test.


class TestProjectFileScan:
    """Contracts for combining recorded and governed scan scopes."""

    def test_nested_governance_roots_are_minimized(self) -> None:
        assert _minimal_governed_prefixes({".agents/", ".agents/skills/", ".claude/skills/"}) == (
            ".agents",
            ".claude/skills",
        )
