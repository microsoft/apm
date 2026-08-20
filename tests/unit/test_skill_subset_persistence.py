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
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens


class TestSkillSubsetFilterTokens:
    """Canonical matching tokens for declared and flattened skill names."""

    @pytest.mark.parametrize(
        ("skill_subset", "expected"),
        [
            pytest.param(None, None, id="none"),
            pytest.param([], None, id="empty"),
            pytest.param(["   "], None, id="blank"),
            pytest.param(
                [" category\\grill-me "],
                {"category\\grill-me", "category/grill-me", "grill-me"},
                id="backslash-prefixed",
            ),
        ],
    )
    def test_normalizes_subset_tokens(
        self,
        skill_subset: list[str] | None,
        expected: set[str] | None,
    ) -> None:
        assert skill_subset_filter_tokens(skill_subset) == expected

    def test_shared_leaf_name_is_one_flattened_token(self) -> None:
        assert skill_subset_filter_tokens(["one/foo", "two/foo"]) == {
            "one/foo",
            "two/foo",
            "foo",
        }

    def test_blank_name_does_not_hide_later_names(self) -> None:
        assert skill_subset_filter_tokens([" ", "category/reviewer"]) == {
            "category/reviewer",
            "reviewer",
        }


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

    @pytest.mark.parametrize("invalid_name", [None, 7, " "])
    def test_parse_skills_rejects_non_string_or_blank_name(self, invalid_name: object) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            DependencyReference.parse_from_dict({"git": "owner/repo", "skills": [invalid_name]})

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
        """Parse dict with skills, emit, re-parse -> same value."""
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

    def test_local_path_parse_skills(self):
        """skills: on a path: dep populates skill_subset (regression for issue #1982)."""
        entry = {"path": "./local", "skills": ["reviewer"]}
        ref = DependencyReference.parse_from_dict(entry)
        assert ref.skill_subset == ["reviewer"]
        assert ref.is_local

    def test_local_path_skills_round_trip(self):
        """path: dep with skills: emits dict form, not git: form."""
        entry = {"path": "./local", "skills": ["reviewer"]}
        emitted = DependencyReference.parse_from_dict(entry).to_apm_yml_entry()
        assert emitted == {"path": "./local", "skills": ["reviewer"]}


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

    def test_flat_list_deps_rejected(self, tmp_path):
        """Flat list dependencies raise ValueError in the writer."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              - owner/repo#main
            """,
        )
        with pytest.raises(ValueError, match=r"expected a mapping.*got list"):
            set_skill_subset_for_entry(manifest, "owner/repo", ["alpha"])

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

    def test_set_target_subset_promotes_string_entry(self, tmp_path):
        """Target writer promotes string entries and persists sorted targets."""
        from apm_cli.commands._apm_yml_writer import set_target_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - owner/repo#main
            """,
        )

        assert set_target_subset_for_entry(manifest, "owner/repo", ["codex", "claude"]) is True
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        assert entry["git"] == "owner/repo"
        assert entry["ref"] == "main"
        assert entry["targets"] == ["claude", "codex"]

    def test_set_target_subset_clears_field_and_preserves_skills(self, tmp_path):
        """Clearing targets leaves existing skills untouched."""
        from apm_cli.commands._apm_yml_writer import set_target_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - git: owner/repo
                  skills:
                    - reviewer
                  targets:
                    - codex
            """,
        )

        assert set_target_subset_for_entry(manifest, "owner/repo", None) is True
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        assert entry["skills"] == ["reviewer"]
        assert "targets" not in entry

    def test_set_target_subset_preserves_existing_skills(self, tmp_path):
        """Setting targets does not remove the existing skills field."""
        from apm_cli.commands._apm_yml_writer import set_target_subset_for_entry
        from apm_cli.utils.yaml_io import load_yaml

        manifest = self._write_manifest(
            tmp_path,
            """\
            dependencies:
              apm:
                - git: owner/repo
                  skills:
                    - reviewer
            """,
        )

        assert set_target_subset_for_entry(manifest, "owner/repo", ["codex"]) is True
        data = load_yaml(manifest)
        entry = data["dependencies"]["apm"][0]
        assert entry["skills"] == ["reviewer"]
        assert entry["targets"] == ["codex"]

    def test_no_dependencies_key_returns_false(self, tmp_path):
        """Manifest with no 'dependencies' key at all returns False (line 30: deps_section={})."""
        from apm_cli.commands._apm_yml_writer import set_skill_subset_for_entry

        manifest = self._write_manifest(tmp_path, "name: test\n")
        result = set_skill_subset_for_entry(manifest, "owner/repo", ["skill"])
        assert result is False

    def test_entry_matches_non_str_non_dict_returns_false(self):
        """Non-str/non-dict entry in _entry_matches returns False (line 70)."""
        from apm_cli.commands._apm_yml_writer import _entry_matches

        assert _entry_matches(42, "owner/repo") is False
        assert _entry_matches(None, "owner/repo") is False
        assert _entry_matches(["owner/repo"], "owner/repo") is False

    def test_entry_matches_parse_error_returns_false(self):
        """ValueError from DependencyReference.parse returns False (lines 72-73)."""
        from unittest.mock import patch

        from apm_cli.commands._apm_yml_writer import _entry_matches

        with patch(
            "apm_cli.commands._apm_yml_writer.DependencyReference.parse",
            side_effect=ValueError("bad ref"),
        ):
            result = _entry_matches("bad-ref-string", "owner/repo")
        assert result is False

    def test_apply_subset_non_str_non_dict_returns_entry_unchanged(self):
        """Non-str/non-dict entry is returned unchanged from _apply_subset (line 84)."""
        from apm_cli.commands._apm_yml_writer import _apply_subset

        entry = 42
        result = _apply_subset(entry, ["skill"])
        assert result == entry

        entry_list = ["owner/repo"]
        result_list = _apply_subset(entry_list, None)
        assert result_list == entry_list


# ============================================================================
# _check_skill_subset_consistency audit check
# ============================================================================


class TestSkillSubsetConsistencyCheck:
    """Test _check_skill_subset_consistency audit check."""

    def _make_manifest_mock(self, deps):
        """Create a manifest mock with given dep_refs."""
        manifest = Mock()
        manifest.get_apm_dependencies.return_value = deps
        manifest.get_dev_apm_dependencies.return_value = []
        manifest.get_all_apm_dependencies.return_value = deps
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


# ============================================================================
# get_existing_skill_subset — read persisted skills: from current_deps
# ============================================================================


class TestGetExistingSkillSubset:
    """Test get_existing_skill_subset helper (issue #1771)."""

    def test_returns_skills_from_dict_entry(self):
        """Dict entry with skills: returns the list."""
        from apm_cli.install.package_resolution import get_existing_skill_subset

        current_deps = [{"git": "owner/repo", "skills": ["alpha", "beta"]}]
        result = get_existing_skill_subset(
            current_deps, "owner/repo", dependency_reference_cls=DependencyReference
        )
        assert result == ["alpha", "beta"]

    def test_returns_none_for_string_entry(self):
        """Plain string entry has no skill_subset -- returns None."""
        from apm_cli.install.package_resolution import get_existing_skill_subset

        current_deps = ["owner/repo"]
        result = get_existing_skill_subset(
            current_deps, "owner/repo", dependency_reference_cls=DependencyReference
        )
        assert result is None

    def test_returns_none_when_identity_not_found(self):
        """No matching identity returns None."""
        from apm_cli.install.package_resolution import get_existing_skill_subset

        current_deps = [{"git": "other/repo", "skills": ["alpha"]}]
        result = get_existing_skill_subset(
            current_deps, "owner/repo", dependency_reference_cls=DependencyReference
        )
        assert result is None

    def test_returns_none_for_empty_deps(self):
        """Empty deps list returns None."""
        from apm_cli.install.package_resolution import get_existing_skill_subset

        result = get_existing_skill_subset(
            [], "owner/repo", dependency_reference_cls=DependencyReference
        )
        assert result is None

    def test_skips_unparseable_entries(self):
        """Unparseable entries are silently skipped."""
        from apm_cli.install.package_resolution import get_existing_skill_subset

        current_deps = [42, {"git": "owner/repo", "skills": ["alpha"]}]
        result = get_existing_skill_subset(
            current_deps, "owner/repo", dependency_reference_cls=DependencyReference
        )
        assert result == ["alpha"]


# ============================================================================
# _resolve_package_references — additive --skill merge (issue #1771)
# ============================================================================


class TestSkillSubsetAdditiveMerge:
    """Verify that --skill merges with existing manifest skills: list."""

    def test_skill_names_merge_with_existing(self):
        """New --skill names are merged with the persisted skills: list."""
        from unittest.mock import patch

        from apm_cli.commands.install import _resolve_package_references

        current_deps = [{"git": "owner/repo", "skills": ["grill-me"]}]
        existing_identities = {"owner/repo"}

        with (
            patch(
                "apm_cli.commands.install._validate_package_exists",
                return_value=True,
            ),
        ):
            (
                valid_outcomes,
                _invalid,
                _validated,
                _mktplace,
                apm_yml_entries,
                _changed,
            ) = _resolve_package_references(
                ["owner/repo"],
                current_deps,
                existing_identities.copy(),
                skill_subset=("qa",),
            )

        # The merged skill list should contain both the existing and new
        assert len(valid_outcomes) == 1
        assert "owner/repo" in apm_yml_entries
        entry = apm_yml_entries["owner/repo"]
        assert isinstance(entry, dict)
        assert sorted(entry["skills"]) == ["grill-me", "qa"]

    def test_skill_names_no_duplicates(self):
        """Re-adding an existing skill name does not produce duplicates."""
        from unittest.mock import patch

        from apm_cli.commands.install import _resolve_package_references

        current_deps = [{"git": "owner/repo", "skills": ["alpha", "beta"]}]
        existing_identities = {"owner/repo"}

        with (
            patch(
                "apm_cli.commands.install._validate_package_exists",
                return_value=True,
            ),
        ):
            (
                _valid,
                _invalid,
                _validated,
                _mktplace,
                apm_yml_entries,
                _changed,
            ) = _resolve_package_references(
                ["owner/repo"],
                current_deps,
                existing_identities.copy(),
                skill_subset=("alpha", "gamma"),
            )

        entry = apm_yml_entries["owner/repo"]
        assert sorted(entry["skills"]) == ["alpha", "beta", "gamma"]

    def test_skill_new_package_no_existing_to_merge(self):
        """First install with --skill on a new package works (no existing)."""
        from unittest.mock import patch

        from apm_cli.commands.install import _resolve_package_references

        current_deps = []
        existing_identities: set[str] = set()

        with (
            patch(
                "apm_cli.commands.install._validate_package_exists",
                return_value=True,
            ),
        ):
            (
                _valid,
                _invalid,
                _validated,
                _mktplace,
                apm_yml_entries,
                _changed,
            ) = _resolve_package_references(
                ["owner/repo"],
                current_deps,
                existing_identities.copy(),
                skill_subset=("qa",),
            )

        entry = apm_yml_entries["owner/repo"]
        assert entry["skills"] == ["qa"]


# ============================================================================
# _resolve_package_references -- registry identity must not silently convert
# to git form (regression: PR #2166 review)
# ============================================================================


class TestResolvePackageReferencesPreservesRegistryForm:
    """A CLI positional matching an existing registry-longhand identity must
    not be silently rewritten to git form when combined with --skill."""

    @pytest.fixture(autouse=True)
    def _enable_package_registry(self, monkeypatch):
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"registries": True}})

    def test_cli_skill_flag_preserves_registry_form(self):
        from unittest.mock import patch

        from apm_cli.commands.install import _resolve_package_references

        current_deps = [{"id": "testorg/demo-pkg", "version": "1.0.0"}]
        existing_identities = {"testorg/demo-pkg"}

        with patch(
            "apm_cli.commands.install._validate_package_exists",
            return_value=True,
        ):
            (
                valid_outcomes,
                invalid_outcomes,
                _validated,
                _mktplace,
                apm_yml_entries,
                _changed,
            ) = _resolve_package_references(
                ["testorg/demo-pkg#1.0.0"],
                current_deps,
                existing_identities.copy(),
                skill_subset=("skill-gamma",),
            )

        assert invalid_outcomes == []
        assert len(valid_outcomes) == 1
        entry = apm_yml_entries["testorg/demo-pkg#1.0.0"]
        assert isinstance(entry, dict)
        assert entry.get("id") == "testorg/demo-pkg"
        assert "git" not in entry
        assert entry["skills"] == ["skill-gamma"]

    def test_current_deps_not_converted_to_git(self):
        """The persisted current_deps list itself must keep the registry shape."""
        from unittest.mock import patch

        from apm_cli.commands.install import _resolve_package_references

        current_deps = [{"id": "testorg/demo-pkg", "version": "1.0.0"}]
        existing_identities = {"testorg/demo-pkg"}

        with patch(
            "apm_cli.commands.install._validate_package_exists",
            return_value=True,
        ):
            _resolve_package_references(
                ["testorg/demo-pkg#1.0.0"],
                current_deps,
                existing_identities.copy(),
                skill_subset=("skill-gamma",),
            )

        # update_existing_dependency_entry_if_needed only rewrites current_deps
        # when apm_yml_entries already holds a merged entry for this canonical;
        # the assertion that matters is that IF it rewrites, it keeps registry shape.
        assert all("git" not in dep for dep in current_deps if isinstance(dep, dict))
        assert any(
            isinstance(dep, dict) and dep.get("id") == "testorg/demo-pkg" for dep in current_deps
        )
