"""Unit tests for ``apm_cli.integration.copilot_app_project``.

Covers:

* ``derive_repo_context`` -- no-repo / no-remote / GitHub-remote /
  detached-HEAD branches.
* ``derive_project_recipe`` -- pure projection.
* ``resolve_or_register_project_sqlite`` -- MISS (INSERT),
  HIT (SELECT-only), race-collision recovery, missing-DB error,
  malformed schema rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration import copilot_app_project as cap

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Schema fixture -- includes the workflows table (required by user_version
# guard) plus the projects table (subject under test).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure derive_* tests.
# ---------------------------------------------------------------------------


class TestDeriveProjectRecipe:
    def test_pure_projection(self) -> None:
        ctx = cap.RepoContext(
            repo_root=Path("/tmp/x"),
            repo_name="x",
            github_owner="o",
            github_repo="x",
            default_branch="main",
        )
        r = cap.derive_project_recipe(ctx)
        assert r.name == "x"
        # ``main_repo_path`` is the OS-native string form of the repo
        # root; on POSIX this is ``/tmp/x``, on Windows ``\tmp\x``.
        assert r.main_repo_path == str(Path("/tmp/x"))
        assert r.default_branch == "main"
        assert r.github_owner == "o"
        assert r.github_repo == "x"
        # Dashed UUID4 -- 36 chars including hyphens.
        assert len(r.id) == 36
        assert r.id.count("-") == 4


# ---------------------------------------------------------------------------
# resolve_or_register_project_sqlite tests.
# ---------------------------------------------------------------------------
