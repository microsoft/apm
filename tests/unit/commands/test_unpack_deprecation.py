"""Unit tests asserting ``apm unpack`` emits a deprecation warning.

Tests the v0.12 deprecation notice added to ``unpack_cmd`` in
``apm_cli.commands.pack``.  Until the production change lands, these tests
will FAIL because the deprecation warning string is not present.
"""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_apm_bundle(tmp_path: Path) -> Path:
    """Create a minimal apm-format bundle with a lockfile for unpack to consume."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    bundle = tmp_path / "test-pkg-1.0.0"
    bundle.mkdir(parents=True)

    # A file the lockfile references
    gh_dir = bundle / ".github" / "skills" / "coding"
    gh_dir.mkdir(parents=True)
    (gh_dir / "SKILL.md").write_text("# Coding Skill", encoding="utf-8")

    # Lockfile
    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/test-pkg",
        resolved_commit="abc123",
        deployed_files=[".github/skills/coding/SKILL.md"],
    )
    lockfile.add_dependency(dep)
    lockfile.write(bundle / "apm.lock.yaml")

    return bundle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUnpackDeprecationWarning:
    """Assert that ``apm unpack`` emits a deprecation warning in v0.12+."""

    def setup_method(self) -> None:
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self) -> None:
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent.parent
            os.chdir(str(repo_root))

    def test_unpack_emits_deprecation_warning(self, tmp_path: Path) -> None:
        """``apm unpack`` must emit a deprecation warning pointing users
        to ``apm install <bundle>``."""
        bundle = _make_apm_bundle(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        os.chdir(output_dir)
        result = self.runner.invoke(
            cli,
            ["unpack", str(bundle), "--dry-run"],
            catch_exceptions=False,
        )

        # The deprecation warning must appear in output.
        # This will FAIL until pack.py is updated with the warning.
        assert (
            "deprecated" in result.output.lower()
            or "deprecated" in (result.stderr if hasattr(result, "stderr") else "").lower()
        ), (
            "Expected deprecation warning in 'apm unpack' output. "
            "Deprecation notice not yet implemented."
        )

    def test_unpack_still_works_after_deprecation(self, tmp_path: Path) -> None:
        """``apm unpack`` must still function normally (behavior unchanged)
        even after the deprecation warning is added."""
        bundle = _make_apm_bundle(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        os.chdir(output_dir)
        result = self.runner.invoke(
            cli,
            ["unpack", str(bundle), "--dry-run"],
            catch_exceptions=False,
        )

        # Dry-run should succeed (exit 0) and show file list
        assert result.exit_code == 0, (
            f"apm unpack --dry-run failed with exit code {result.exit_code}: {result.output}"
        )
