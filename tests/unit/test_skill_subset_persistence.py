"""Unit tests for Phase 11: skill subset persistence.

Covers:
- DependencyReference parsing / serialization of `skills:` field
- LockedDependency skill_subset round-trip
- _apm_yml_writer.set_skill_subset_for_entry
- _check_skill_subset_consistency audit check
"""

import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.models.dependency.reference import DependencyReference

# ============================================================================
# DependencyReference — parse_from_dict with skills: field
# ============================================================================


class TestDependencyReferenceSkillSubset:
    """Test skills: field in parse_from_dict and to_apm_yml_entry."""

    def test_parse_skills_field(self):
        """skills: [a, b] populates skill_subset."""
        entry = {"git": "owner/repo", "skills": ["alpha", "beta"]}
        ref = DependencyReference.parse_from_dict(entry)
        assert ref.skill_subset == ["alpha", "beta"]

    def test_parse_no_skills_field(self):
        """Missing skills: leaves skill_subset as None."""
        entry = {"git": "owner/repo"}
        ref = DependencyReference.parse_from_dict(entry)
        assert ref.skill_subset is None

    def test_parse_skills_sorts_and_dedupes(self):
        """skills: is sorted and deduped on parse."""
        entry = {"git": "owner/repo", "skills": ["gamma", "alpha", "gamma", "beta"]}
        ref = DependencyReference.parse_from_dict(entry)
        assert ref.skill_subset == ["alpha", "beta", "gamma"]

    def test_parse_skills_empty_list_raises(self):
        """skills: [] raises ValueError (Security requirement)."""
        entry = {"git": "owner/repo", "skills": []}
        with pytest.raises(ValueError, match="must contain at least one"):
            DependencyReference.parse_from_dict(entry)

    def test_parse_skills_path_traversal_rejects(self):
        """Skill name with path traversal is rejected."""
        entry = {"git": "owner/repo", "skills": ["../evil"]}
        with pytest.raises(ValueError, match="traversal"):
            DependencyReference.parse_from_dict(entry)

    def test_parse_skills_with_dot_dot_rejects(self):
        """Skill name with '..' segment is rejected."""
        entry = {"git": "owner/repo", "skills": ["foo/../bar"]}
        with pytest.raises(ValueError, match="traversal"):
            DependencyReference.parse_from_dict(entry)

    def test_to_apm_yml_entry_with_skills(self):
        """to_apm_yml_entry emits dict with skills: when skill_subset is set."""
        entry = {"git": "owner/repo", "skills": ["alpha", "beta"]}
        ref = DependencyReference.parse_from_dict(entry)
        result = ref.to_apm_yml_entry()
        assert isinstance(result, dict)
        assert result["skills"] == ["alpha", "beta"]
        assert "git" in result

    def test_to_apm_yml_entry_without_skills_is_string(self):
        """to_apm_yml_entry returns plain string when no skill_subset."""
        entry = {"git": "owner/repo"}
        ref = DependencyReference.parse_from_dict(entry)
        result = ref.to_apm_yml_entry()
        assert isinstance(result, str)
        assert "owner/repo" in result

    def test_round_trip_parse_emit(self):
        """Parse dict with skills, emit, re-parse → same value."""
        entry = {"git": "owner/repo#main", "skills": ["web", "cli"]}
        ref = DependencyReference.parse_from_dict(entry)
        emitted = ref.to_apm_yml_entry()
        assert isinstance(emitted, dict)
        ref2 = DependencyReference.parse_from_dict(emitted)
        assert ref2.skill_subset == ["cli", "web"]
        assert ref2.repo_url == "owner/repo"

    def test_skills_with_ref_field(self):
        """skills: works with ref: field."""
        entry = {"git": "owner/repo", "ref": "v2.0.0", "skills": ["my-skill"]}
        ref = DependencyReference.parse_from_dict(entry)
        assert ref.skill_subset == ["my-skill"]
        assert ref.reference == "v2.0.0"


# ============================================================================
# LockedDependency — skill_subset round-trip
# ============================================================================


class TestLockedDependencySkillSubset:
    """Test skill_subset on LockedDependency."""

    def test_to_dict_with_subset(self):
        """skill_subset is emitted in to_dict when non-empty."""
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_ref="main",
            resolved_commit="abc123",
            skill_subset=["alpha", "beta"],
        )
        d = dep.to_dict()
        assert d["skill_subset"] == ["alpha", "beta"]

    def test_to_dict_without_subset(self):
        """skill_subset is omitted from to_dict when empty."""
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_ref="main",
            resolved_commit="abc123",
            skill_subset=[],
        )
        d = dep.to_dict()
        assert "skill_subset" not in d

    def test_from_dict_with_subset(self):
        """from_dict reads skill_subset."""
        d = {
            "repo_url": "owner/repo",
            "resolved_ref": "main",
            "resolved_commit": "abc123",
            "skill_subset": ["alpha", "beta"],
        }
        dep = LockedDependency.from_dict(d)
        assert dep.skill_subset == ["alpha", "beta"]

    def test_from_dict_without_subset_backward_compat(self):
        """from_dict without skill_subset defaults to empty list."""
        d = {
            "repo_url": "owner/repo",
            "resolved_ref": "main",
            "resolved_commit": "abc123",
        }
        dep = LockedDependency.from_dict(d)
        assert dep.skill_subset == []

    def test_from_dependency_ref_copies_subset(self):
        """from_dependency_ref copies skill_subset from dep_ref."""
        ref = DependencyReference.parse("owner/repo#main")
        ref.skill_subset = ["cli", "web"]
        locked = LockedDependency.from_dependency_ref(
            dep_ref=ref,
            resolved_commit="abc123",
            depth=0,
            resolved_by="direct",
        )
        assert locked.skill_subset == ["cli", "web"]

    def test_from_dependency_ref_no_subset(self):
        """from_dependency_ref with None skill_subset → empty list."""
        ref = DependencyReference.parse("owner/repo#main")
        ref.skill_subset = None
        locked = LockedDependency.from_dependency_ref(
            dep_ref=ref,
            resolved_commit="abc123",
            depth=0,
            resolved_by="direct",
        )
        assert locked.skill_subset == []


# ============================================================================
# _apm_yml_writer.set_skill_subset_for_entry
# ============================================================================


class TestApmYmlWriter:
    """Test set_skill_subset_for_entry helper."""

    def _write_manifest(self, tmp_path: Path, content: str) -> Path:
        manifest = tmp_path / "apm.yml"
        manifest.write_text(textwrap.dedent(content))
        return manifest

    def test_string_promoted_to_dict_with_skills(self, tmp_path):
        """String entry is promoted to dict form when skills are set."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - owner/repo#main
            """,
        )
        result = set_skill_subset_for_entry(manifest, "owner/repo", ["alpha", "beta"])
        assert result is True
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        assert isinstance(entry, dict)
        assert entry["skills"] == ["alpha", "beta"]

    def test_clear_skills_reverts_to_string(self, tmp_path):
        """Setting subset=None clears skills and reverts to string form."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - git: owner/repo
                  ref: main
                  skills:
                    - alpha
                    - beta
            """,
        )
        result = set_skill_subset_for_entry(manifest, "owner/repo", None)
        assert result is True
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        # Should be string form (no skills, no insecure → string)
        assert isinstance(entry, str)
        assert "owner/repo" in entry

    def test_non_matching_repo_not_modified(self, tmp_path):
        """Non-matching repo_url leaves file unchanged."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - owner/other-repo#main
            """,
        )
        result = set_skill_subset_for_entry(manifest, "owner/repo", ["alpha"])
        assert result is False

    def test_empty_deps_returns_false(self, tmp_path):
        """No apm deps returns False."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies: {}
            """,
        )
        result = set_skill_subset_for_entry(manifest, "owner/repo", ["alpha"])
        assert result is False

    def test_update_existing_dict_entry(self, tmp_path):
        """Dict entry with existing skills: gets updated."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - git: owner/repo
                  ref: main
                  skills:
                    - old-skill
            """,
        )
        result = set_skill_subset_for_entry(manifest, "owner/repo", ["new-a", "new-b"])
        assert result is True
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        assert entry["skills"] == ["new-a", "new-b"]

    def test_subset_is_sorted_and_deduped(self, tmp_path):
        """Writer sorts and dedupes the subset."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - owner/repo#main
            """,
        )
        set_skill_subset_for_entry(manifest, "owner/repo", ["gamma", "alpha", "gamma"])
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        assert entry["skills"] == ["alpha", "gamma"]


# ============================================================================
# _check_skill_subset_consistency audit check
# ============================================================================


class TestSkillSubsetConsistencyCheck:
    """Test _check_skill_subset_consistency audit check."""

    def _make_manifest_mock(self, deps):
        """Create a manifest mock with given dep_refs."""
        manifest = Mock()
        manifest.get_apm_dependencies.return_value = deps
        return manifest

    def _make_lock_mock(self, locked_deps):
        """Create a lock mock: get_dependency(key) → locked_dep."""
        lock = Mock()
        lock.get_dependency = lambda key: locked_deps.get(key)
        return lock

    def _make_dep_ref(self, repo_url, skill_subset=None):
        ref = Mock()
        ref.get_unique_key.return_value = repo_url
        ref.skill_subset = skill_subset
        return ref

    def _make_locked_dep(self, package_type="skill_bundle", skill_subset=None):
        dep = Mock()
        dep.package_type = package_type
        dep.skill_subset = skill_subset if skill_subset else []
        return dep

    def test_consistent_passes(self):
        """Matching skill_subset → check passes."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha", "beta"])
        locked = self._make_locked_dep(skill_subset=["alpha", "beta"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock)
        assert result.passed is True

    def test_mismatch_fails(self):
        """Different skill_subset → check fails."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha", "beta"])
        locked = self._make_locked_dep(skill_subset=["alpha"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock)
        assert result.passed is False
        assert "mismatch" in result.message

    def test_no_manifest_subset_vs_lock_subset_fails(self):
        """Manifest has no skills: but lockfile has skill_subset → fails."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=None)
        locked = self._make_locked_dep(skill_subset=["alpha"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock)
        assert result.passed is False

    def test_non_bundle_skipped(self):
        """Non skill_bundle packages are skipped."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha"])
        locked = self._make_locked_dep(package_type="marketplace_plugin", skill_subset=[])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock)
        assert result.passed is True

    def test_missing_from_lock_skipped(self):
        """Deps not in lockfile are skipped (other checks catch this)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({})

        result = _check_skill_subset_consistency(manifest, lock)
        assert result.passed is True

    def test_both_empty_passes(self):
        """Both manifest and lockfile with no skill_subset → passes."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=None)
        locked = self._make_locked_dep(skill_subset=[])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock)
        assert result.passed is True
