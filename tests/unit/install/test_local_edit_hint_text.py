"""Local-edit hint wording."""

from __future__ import annotations

import pytest

from apm_cli.install.drift import DriftFinding, render_drift_text
from apm_cli.install.local_edits import (
    local_edit_hint,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]


class TestDriftTextHint:
    def test_modified_findings_add_export_hint(self) -> None:
        out = render_drift_text(
            [
                DriftFinding(path=".github/a.md", kind="modified", package="org/pkg"),
                DriftFinding(path=".github/b.md", kind="modified", package="org/pkg"),
            ]
        )
        assert local_edit_hint(2) in out
        # The hint must precede the restore advice that would overwrite edits,
        # which deletes the edited files first (#3994, RA-009).
        assert out.index("apm export-patch") < out.index("Delete the edited files")
        assert "Run 'apm install' to re-sync" not in out

    def test_bundle_file_restore_names_the_bundle_reinstall(self) -> None:
        """'apm install' never redeploys bundle files; reinstalling the bundle does (#3994)."""
        out = render_drift_text(
            [DriftFinding(path=".claude/agents/a.md", kind="modified", package="local-bundle")]
        )
        flat = " ".join(out.split())
        assert (
            "then run 'apm install <bundle-path>' to reinstall the bundle that deployed them"
            in flat
        )
        assert "rerun 'apm install'," not in flat

    def test_project_local_and_unintegrated_findings_get_no_hint(self) -> None:
        out = render_drift_text(
            [
                DriftFinding(path="AGENTS.md", kind="modified", package="."),
                DriftFinding(path=".github/c.md", kind="unintegrated", package="org/pkg"),
            ]
        )
        assert "apm export-patch" not in out
