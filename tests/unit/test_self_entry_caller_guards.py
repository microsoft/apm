"""Regression tests ensuring callers do not crash on the virtual self-entry.

Issue #887 introduced a synthesized "." self-entry in the lockfile representing
the project's own local content. Callers that interpret entries as installable
remote packages (paths via to_dependency_ref(), apm_modules/<repo_url>, etc.)
must skip it. These tests pin the contract for representative call sites.
"""

from pathlib import Path  # noqa: F401
from unittest.mock import patch

from apm_cli.commands.uninstall.engine import _validate_uninstall_packages
from apm_cli.core.command_logger import CommandLogger
from apm_cli.deps.lockfile import _SELF_KEY, LockedDependency, LockFile
from apm_cli.integration.skill_integrator import SkillIntegrator


def _lockfile_with_self_and_remote() -> LockFile:
    lock = LockFile(
        lockfile_version="1",
        generated_at="2025-01-01T00:00:00+00:00",
        apm_version="0.0.0-test",
    )
    lock.add_dependency(
        LockedDependency(
            repo_url="owner/repo",
            resolved_commit="a" * 40,
            depth=1,
            deployed_files=[".github/skills/remote-skill/SKILL.md"],
            deployed_file_hashes={".github/skills/remote-skill/SKILL.md": "deadbeef"},
        )
    )
    lock.local_deployed_files = [".github/skills/local-skill/SKILL.md"]
    lock.local_deployed_file_hashes = {
        ".github/skills/local-skill/SKILL.md": "1111111111111111",
    }
    # Round-trip so the synthesized "." entry is materialized.
    return LockFile.from_yaml(lock.to_yaml())


# ---------------------------------------------------------------------------
# get_installed_paths() must skip the self-entry
# ---------------------------------------------------------------------------


class TestGetInstalledPathsSkipsSelfEntry:
    def test_self_entry_not_in_installed_paths(self, tmp_path):
        lock = _lockfile_with_self_and_remote()
        # Sanity: the self-entry must be present in raw dependencies.
        assert _SELF_KEY in lock.dependencies

        paths = lock.get_installed_paths(tmp_path / "apm_modules")

        # The self-entry's local_path is "." which would resolve to the
        # apm_modules dir itself; ensure it is filtered out.
        assert "." not in paths
        assert "owner/repo" in paths

    def test_only_self_entry_returns_empty(self, tmp_path):
        lock = LockFile(
            lockfile_version="1",
            generated_at="2025-01-01T00:00:00+00:00",
        )
        lock.local_deployed_files = [".github/skills/local/SKILL.md"]
        lock.local_deployed_file_hashes = {
            ".github/skills/local/SKILL.md": "abc",
        }
        lock = LockFile.from_yaml(lock.to_yaml())

        # Must not crash trying to parse "<self>" as a repo URL.
        paths = lock.get_installed_paths(tmp_path / "apm_modules")
        assert paths == []


# ---------------------------------------------------------------------------
# apm uninstall <self-key> must not crash
# ---------------------------------------------------------------------------


class TestUninstallRejectsSelfKey:
    def test_uninstall_dot_does_not_crash(self):
        """Trying to uninstall the literal '.' returns 'not found', no crash."""
        logger = CommandLogger(command="uninstall", verbose=False)
        # current_deps are real apm.yml dependency entries; "." is not one.
        to_remove, not_found = _validate_uninstall_packages(  # noqa: RUF059
            packages=["."],
            current_deps=["owner/repo"],
            logger=logger,
        )
        # "." has no "/", so the validator rejects it as invalid format and
        # skips it entirely - never reaches the not-found list.
        assert to_remove == []
        assert "." not in to_remove


# ---------------------------------------------------------------------------
# SkillIntegrator ownership-map iteration must skip the self-entry
# ---------------------------------------------------------------------------


class TestSkillIntegratorSkipsSelfEntry:
    def test_ownership_map_excludes_self_entry(self, tmp_path):
        """The self-entry's local skills must not pollute the package-owner map.

        The self-entry has repo_url='<self>' / virtual_path=None which would
        give a bogus short_owner of '<self>'. It must be filtered out.
        """
        lock = _lockfile_with_self_and_remote()
        # The self-entry has a deployed skill file that would otherwise land
        # in owned_by under owner '<self>'.

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock",
            ),
        ):
            owned_by, native_owners = SkillIntegrator._build_ownership_maps(tmp_path)

        # Self-entry's owner string would have been '<self>' if not skipped.
        assert "<self>" not in owned_by.values()
        assert "<self>" not in native_owners.values()
        # The remote dep's skill leaf-name is 'SKILL.md' (last path segment).
        assert owned_by.get("SKILL.md") == "repo"
