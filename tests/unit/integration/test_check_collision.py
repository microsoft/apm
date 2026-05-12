"""Regression tests for BaseIntegrator.check_collision() -- managed_files=None semantics.

Covers the latent defect where managed_files=None bypassed collision detection
entirely, silently overwriting user-authored governance files on first install.

After the fix, None is treated as an empty set: no file is managed, so any
pre-existing file at the target path is a collision (protected from overwrite).

Scenarios:
    1. managed_files=None + file exists + force=False  -> collision (True)
    2. managed_files=None + file absent               -> no collision (False)
    3. managed_files=set() + file exists + force=False -> collision (True)
    4. managed_files contains rel_path + file exists  -> no collision (False)
    5. Integration: integrate_instructions_for_target skips hand-rolled file
       when managed_files=None and the file already exists on disk.
"""

from datetime import datetime
from pathlib import Path

from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_info(package_dir: Path, name: str = "test-pkg") -> PackageInfo:
    """Build a minimal PackageInfo for use in integration-level tests."""
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
        source=f"github.com/test/{name}",
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# Unit-level: check_collision seam
# ---------------------------------------------------------------------------


class TestCheckCollisionNoneSemantics:
    """managed_files=None must behave identically to managed_files=set()."""

    def test_none_with_existing_file_no_force_is_collision(self, tmp_path: Path) -> None:
        """managed_files=None + file exists + force=False -> collision detected."""
        target = tmp_path / "handrolled.instructions.md"
        target.write_text("# hand-rolled content")

        result = BaseIntegrator.check_collision(
            target, ".github/instructions/handrolled.instructions.md", None, False
        )

        assert result is True, "None managed_files must NOT bypass collision detection"

    def test_none_with_absent_file_no_collision(self, tmp_path: Path) -> None:
        """managed_files=None + file absent -> safe to deploy (no collision)."""
        target = tmp_path / "new.instructions.md"
        # File intentionally not created

        result = BaseIntegrator.check_collision(
            target, ".github/instructions/new.instructions.md", None, False
        )

        assert result is False

    def test_empty_set_with_existing_file_is_collision(self, tmp_path: Path) -> None:
        """managed_files=set() + file exists -> collision.

        Verifies that None-as-empty-set behavior matches an explicit empty set.
        """
        target = tmp_path / "handrolled.instructions.md"
        target.write_text("# hand-rolled content")

        result = BaseIntegrator.check_collision(
            target, ".github/instructions/handrolled.instructions.md", set(), False
        )

        assert result is True

    def test_none_matches_empty_set_for_existing_file(self, tmp_path: Path) -> None:
        """None and set() produce identical results for an existing file."""
        target = tmp_path / "file.instructions.md"
        target.write_text("# content")
        rel = ".github/instructions/file.instructions.md"

        result_none = BaseIntegrator.check_collision(target, rel, None, False)
        result_empty = BaseIntegrator.check_collision(target, rel, set(), False)

        assert result_none == result_empty

    def test_managed_file_in_set_no_collision(self, tmp_path: Path) -> None:
        """File exists AND rel_path is in managed_files -> not a collision (can update)."""
        target = tmp_path / "apm-managed.instructions.md"
        target.write_text("# APM-managed content")
        rel = ".github/instructions/apm-managed.instructions.md"

        result = BaseIntegrator.check_collision(target, rel, {rel}, False)

        assert result is False

    def test_none_force_true_no_collision(self, tmp_path: Path) -> None:
        """force=True always suppresses the collision, even when managed_files=None."""
        target = tmp_path / "handrolled.instructions.md"
        target.write_text("# hand-rolled content")

        result = BaseIntegrator.check_collision(
            target, ".github/instructions/handrolled.instructions.md", None, True
        )

        assert result is False


# ---------------------------------------------------------------------------
# Integration-level: instruction integrator flow
# ---------------------------------------------------------------------------


class TestIntegrateInstructionsNoneManagedFiles:
    """End-to-end: hand-rolled file is NOT overwritten when managed_files=None."""

    def test_handrolled_file_skipped_when_managed_files_none(self, tmp_path: Path) -> None:
        """integrate_instructions_for_target must skip a pre-existing file.

        Scenario:
          - Package ships handrolled.instructions.md
          - A hand-rolled copy already exists at .github/instructions/handrolled.instructions.md
          - managed_files=None (first install, no lockfile yet)
          - Expected: the hand-rolled file is NOT in target_paths (not overwritten)
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        # Set up package with one instruction file
        pkg_dir = tmp_path / "pkg"
        inst_dir = pkg_dir / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "handrolled.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\n# APM-provided rules"
        )

        # Pre-existing hand-rolled file at the deploy target
        github_instructions = tmp_path / ".github" / "instructions"
        github_instructions.mkdir(parents=True)
        handrolled_path = github_instructions / "handrolled.instructions.md"
        handrolled_path.write_text("# user-authored content -- must not be overwritten")

        pkg_info = _make_package_info(pkg_dir)
        integrator = InstructionIntegrator()
        copilot = KNOWN_TARGETS["copilot"]

        result = integrator.integrate_instructions_for_target(
            copilot,
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        # The hand-rolled file must NOT appear in target_paths
        assert handrolled_path not in result.target_paths, (
            "Hand-rolled file must be skipped when managed_files=None (treated as empty set)"
        )
        # The original content must be preserved on disk
        assert handrolled_path.read_text() == "# user-authored content -- must not be overwritten"
        # The file should have been counted as skipped
        assert result.files_skipped >= 1

    def test_apm_file_deployed_when_path_is_absent(self, tmp_path: Path) -> None:
        """When no pre-existing file exists, managed_files=None still allows deploy."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = tmp_path / "pkg"
        inst_dir = pkg_dir / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\n# Python rules"
        )

        # Ensure .github exists but the target file does NOT
        (tmp_path / ".github" / "instructions").mkdir(parents=True)

        pkg_info = _make_package_info(pkg_dir)
        integrator = InstructionIntegrator()
        copilot = KNOWN_TARGETS["copilot"]

        result = integrator.integrate_instructions_for_target(
            copilot,
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        deployed = tmp_path / ".github" / "instructions" / "python.instructions.md"
        assert deployed in result.target_paths
        assert result.files_integrated == 1
        assert result.files_skipped == 0
