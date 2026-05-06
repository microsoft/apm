"""Unit tests for SkillIntegrator.sync_integration handling cowork:// entries.

Covers the fix for PR #926: cowork://skills/... lockfile entries were silently
skipped during uninstall because sync_integration's prefix tuple only included
local directory prefixes (e.g. .github/skills/) and never matched the
cowork:// URI scheme.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from apm_cli.integration.skill_integrator import SkillIntegrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cowork_target_profile():
    """Return a minimal TargetProfile that mimics the copilot-cowork target."""
    from apm_cli.integration.targets import PrimitiveMapping, TargetProfile

    return TargetProfile(
        name="copilot-cowork",
        root_dir="copilot-cowork",
        primitives={
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
        },
        auto_create=False,
        detect_by_dir=False,
        user_supported=True,
        user_root_resolver=lambda: None,  # will be patched per-test
    )


def _make_copilot_target_profile(project_root: Path):
    """Return a minimal copilot TargetProfile (local, non-cowork)."""
    from apm_cli.integration.targets import PrimitiveMapping, TargetProfile

    return TargetProfile(
        name="copilot",
        root_dir=".github",
        primitives={
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
        },
    )


def _stub_apm_package():
    """Return a minimal APMPackage mock for sync_integration."""
    pkg = MagicMock()
    pkg.get_apm_dependencies.return_value = []
    return pkg


# ---------------------------------------------------------------------------
# Tests: cowork entry in managed_files -> file/dir deleted
# ---------------------------------------------------------------------------


class TestSyncIntegrationCoworkDeletion:
    """sync_integration must resolve cowork:// entries and delete them."""

    def test_cowork_skill_directory_deleted(self, tmp_path: Path) -> None:
        """A cowork://skills/foo entry pointing to a directory is rmtree'd."""
        cowork_root = tmp_path / "cowork-skills"
        skill_dir = cowork_root / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Foo\n", encoding="ascii")
        (skill_dir / "extra.md").write_text("ref\n", encoding="ascii")

        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        targets = [_make_cowork_target_profile()]

        with patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ):
            result = integrator.sync_integration(
                _stub_apm_package(),
                project_root,
                managed_files={"cowork://skills/foo"},
                targets=targets,
            )

        assert not skill_dir.exists(), "Cowork skill directory still exists after sync_integration."
        assert result["files_removed"] == 1
        assert result["errors"] == 0

    def test_cowork_skill_file_deleted(self, tmp_path: Path) -> None:
        """A cowork://skills/bar/SKILL.md entry pointing to a file is unlinked."""
        cowork_root = tmp_path / "cowork-skills"
        skill_dir = cowork_root / "bar"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# Bar\n", encoding="ascii")

        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        targets = [_make_cowork_target_profile()]

        with patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ):
            result = integrator.sync_integration(
                _stub_apm_package(),
                project_root,
                managed_files={"cowork://skills/bar/SKILL.md"},
                targets=targets,
            )

        assert not skill_md.exists(), "Cowork SKILL.md still exists after sync_integration."
        assert result["files_removed"] == 1
        assert result["errors"] == 0


# ---------------------------------------------------------------------------
# Tests: cowork resolver returns None -> graceful skip + warning
# ---------------------------------------------------------------------------


class TestSyncIntegrationCoworkResolverNone:
    """When cowork root resolver returns None, entries are skipped with a warning."""

    def test_resolver_none_skips_entry_and_warns(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        targets = [_make_cowork_target_profile()]

        with (
            patch(
                "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
                return_value=None,
            ),
            patch(
                "apm_cli.utils.console._rich_warning",
            ) as mock_warn,
        ):
            result = integrator.sync_integration(
                _stub_apm_package(),
                project_root,
                managed_files={"cowork://skills/baz"},
                targets=targets,
            )

        # Entry is skipped, not an error.
        assert result["files_removed"] == 0
        assert result["errors"] == 0

        # A one-time warning must have been emitted.
        mock_warn.assert_called_once()
        warn_msg = mock_warn.call_args[0][0]
        assert "OneDrive" in warn_msg or "cowork" in warn_msg.lower()


# ---------------------------------------------------------------------------
# Tests: translation error -> graceful skip (counted as error)
# ---------------------------------------------------------------------------


class TestSyncIntegrationCoworkTranslationError:
    """When from_lockfile_path raises, the entry is skipped and counted as error."""

    def test_translation_error_skips_entry(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()

        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        targets = [_make_cowork_target_profile()]

        with (
            patch(
                "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
                return_value=cowork_root,
            ),
            patch(
                "apm_cli.integration.copilot_cowork_paths.from_lockfile_path",
                side_effect=ValueError("bad path"),
            ),
        ):
            result = integrator.sync_integration(
                _stub_apm_package(),
                project_root,
                managed_files={"cowork://skills/malformed-entry"},
                targets=targets,
            )

        assert result["files_removed"] == 0
        assert result["errors"] == 1


# ---------------------------------------------------------------------------
# Tests: idempotent -- missing file = success
# ---------------------------------------------------------------------------


class TestSyncIntegrationCoworkIdempotent:
    """If the cowork file/dir is already gone, sync_integration succeeds silently."""

    def test_missing_cowork_entry_is_noop(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork-skills"
        cowork_root.mkdir()
        # No skill directory on disk -- it's already been removed.

        project_root = tmp_path / "project"
        project_root.mkdir()

        integrator = SkillIntegrator()
        targets = [_make_cowork_target_profile()]

        with patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ):
            result = integrator.sync_integration(
                _stub_apm_package(),
                project_root,
                managed_files={"cowork://skills/already-gone"},
                targets=targets,
            )

        assert result["files_removed"] == 0
        assert result["errors"] == 0


# ---------------------------------------------------------------------------
# Tests: mixed cowork + local entries
# ---------------------------------------------------------------------------


class TestSyncIntegrationMixed:
    """Both cowork:// and local entries are handled in one call."""

    def test_mixed_entries_all_deleted(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork-skills"
        cowork_skill = cowork_root / "remote-skill"
        cowork_skill.mkdir(parents=True)
        (cowork_skill / "SKILL.md").write_text("# Remote\n", encoding="ascii")

        project_root = tmp_path / "project"
        local_skill = project_root / ".github" / "skills" / "local-skill"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text("# Local\n", encoding="ascii")

        integrator = SkillIntegrator()
        targets = [
            _make_copilot_target_profile(project_root),
            _make_cowork_target_profile(),
        ]

        managed = {
            ".github/skills/local-skill",
            "cowork://skills/remote-skill",
        }

        with patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ):
            result = integrator.sync_integration(
                _stub_apm_package(),
                project_root,
                managed_files=managed,
                targets=targets,
            )

        assert not local_skill.exists(), "Local skill dir should be removed."
        assert not cowork_skill.exists(), "Cowork skill dir should be removed."
        assert result["files_removed"] == 2
        assert result["errors"] == 0
