"""Regression tests for the windsurf uninstall-cleanup bug (#1481):
``apm uninstall`` silently failed to remove deployed skill directories
that windsurf owned.

The original fix (#1486) dropped the ``agents`` primitive from the
windsurf ``TargetProfile`` so the skills deploy path is owned
exclusively by the ``skills`` primitive -- preventing a shadowing
``agents`` bucket from skipping directory cleanup.

apm#1520 then converged windsurf skills onto the cross-tool
``.agents/skills/<name>/SKILL.md`` path (Cascade discovers
``.agents/skills/`` natively), so these tests pin the post-convergence
shape of the windsurf profile and the directory-aware cleanup path.
A future regression -- e.g. re-introducing an ``agents`` primitive that
aliases the same deploy path, or reverting the ``.agents/`` convergence
without updating cleanup routing -- is caught here instead of silently
corrupting an end-user workspace.
"""

from pathlib import Path

from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS


class TestWindsurfTargetProfileShape:
    """windsurf's TargetProfile must not re-introduce the path collision."""

    def test_windsurf_does_not_expose_agents_primitive(self):
        """windsurf intentionally has no 'agents' primitive: Cascade reads
        SKILL.md uniformly, so a separate agents primitive would re-create
        the skills-path collision that caused the silent uninstall bug."""
        windsurf = KNOWN_TARGETS["windsurf"]
        assert "agents" not in windsurf.primitives, (
            "windsurf must not declare an 'agents' primitive: it shares the "
            "skills deploy path with the 'skills' primitive and would "
            "re-introduce the silent uninstall-cleanup bug."
        )

    def test_windsurf_skills_primitive_uses_standard_format(self):
        """windsurf 'skills' primitive uses the standard skill_standard
        format, converged onto the cross-tool ``.agents/skills/`` directory
        (apm#1520)."""
        windsurf = KNOWN_TARGETS["windsurf"]
        skills = windsurf.primitives["skills"]
        assert skills.subdir == "skills"
        assert skills.extension == "/SKILL.md"
        assert skills.format_id == "skill_standard"
        assert skills.deploy_root == ".agents", (
            "windsurf skills converge on .agents/skills/ (apm#1520); "
            f"got deploy_root={skills.deploy_root!r}"
        )


class TestWindsurfPartitionRouting:
    """partition_managed_files must route windsurf's converged
    ``.agents/skills/`` paths to the cross-target 'skills' bucket -- not to a
    windsurf-specific agents bucket."""

    def test_windsurf_skill_path_routes_to_skills_bucket(self):
        """The converged lockfile path '.agents/skills/<name>' must land in the
        'skills' bucket so SkillIntegrator (directory-aware) handles it."""
        managed = {
            ".agents/skills/code-review",
            ".agents/skills/grill-me",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)

        assert ".agents/skills/code-review" in buckets["skills"], (
            "windsurf's converged skill path must be in the cross-target 'skills' bucket"
        )
        assert ".agents/skills/grill-me" in buckets["skills"]

    def test_no_agents_windsurf_bucket_is_created(self):
        """The 'agents_windsurf' bucket must not exist: windsurf no longer
        declares an 'agents' primitive."""
        buckets = BaseIntegrator.partition_managed_files(set())
        assert "agents_windsurf" not in buckets

    def test_windsurf_skill_path_not_routed_to_other_buckets(self):
        """A converged skill path must NOT leak into instructions/commands/
        hooks buckets (which would mean the prefix trie matched the wrong
        primitive)."""
        managed = {".agents/skills/my-skill"}
        buckets = BaseIntegrator.partition_managed_files(managed)

        for bucket_name, paths in buckets.items():
            if bucket_name == "skills":
                continue
            assert ".agents/skills/my-skill" not in paths, (
                f"converged windsurf skill path leaked into bucket '{bucket_name}'"
            )


class TestWindsurfSkillUninstallCleanup:
    """End-to-end: SkillIntegrator.sync_integration must remove the converged
    .agents/skills/<name>/ directories that install deployed for windsurf."""

    def test_sync_removes_converged_skill_directories(self, tmp_path: Path):
        """Regression: skill dirs under the converged .agents/skills/ created
        at install time must be removed when listed in managed_files."""
        skills_root = tmp_path / ".agents" / "skills"
        skills_root.mkdir(parents=True)

        managed_a = skills_root / "code-review"
        managed_a.mkdir()
        (managed_a / "SKILL.md").write_text("managed by APM\n")

        managed_b = skills_root / "grill-me"
        managed_b.mkdir()
        (managed_b / "SKILL.md").write_text("managed by APM\n")

        # User-authored / foreign skill in the same directory must not be touched.
        user_skill = skills_root / "my-custom"
        user_skill.mkdir()
        (user_skill / "SKILL.md").write_text("authored by user\n")

        managed = {
            ".agents/skills/code-review",
            ".agents/skills/grill-me",
        }
        stats = SkillIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        # Primary assertions: filesystem outcomes (the actual user-visible
        # contract). The 'files_removed' stats key is the cross-integrator
        # counter convention (see base_integrator, hook_integrator, etc.)
        # and counts directories here -- we keep a sanity check on it but
        # do not couple the test to its semantics.
        assert not managed_a.exists(), "managed skill dir 'code-review' must be removed"
        assert not managed_b.exists(), "managed skill dir 'grill-me' must be removed"
        assert user_skill.exists(), "foreign/user-authored skill dir must be preserved"
        assert (user_skill / "SKILL.md").read_text() == "authored by user\n"
        assert stats["errors"] == 0
        assert stats["files_removed"] == 2

    def test_sync_handles_trailing_slash_in_managed_path(self, tmp_path: Path):
        """Lockfile entries may carry a trailing slash on directory paths;
        cleanup must work either way."""
        skills_root = tmp_path / ".agents" / "skills"
        skills_root.mkdir(parents=True)
        skill = skills_root / "code-review"
        skill.mkdir()
        (skill / "SKILL.md").write_text("managed")

        managed = {".agents/skills/code-review/"}
        stats = SkillIntegrator().sync_integration(None, tmp_path, managed_files=managed)

        # Primary assertion: the directory is gone regardless of how the
        # integrator counts it internally.
        assert not skill.exists(), (
            "skill dir must be removed even when lockfile path has trailing slash"
        )
        assert stats["errors"] == 0
        assert stats["files_removed"] == 1
