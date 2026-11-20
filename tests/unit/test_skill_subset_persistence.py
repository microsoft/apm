"""Unit tests for Phase 11: skill subset persistence.

Covers:
- DependencyReference parsing / serialization of `skills:` field
- LockedDependency skill_subset round-trip
- _apm_yml_writer.set_skill_subset_for_entry
- _check_skill_subset_consistency audit check
"""

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens

pytestmark = pytest.mark.unit


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


# ============================================================================
# _check_skill_subset_consistency audit check
# ============================================================================


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


# ---------------------------------------------------------------------------
# Unit: --skill overlay on an exact skills x targets pairing (#3763)
# ---------------------------------------------------------------------------


class TestCliOverlayKeepsExactPairing:
    """The CLI overlay is one more edge over the dependency's targets (req-rs-018)."""

    @staticmethod
    def _pairing():
        from apm_cli.models.dependency.subsets import merge_edge_pairings

        return merge_edge_pairings([(["alpha"], ["claude"]), (["gamma"], ["copilot"])])

    def _dep(self):
        from types import SimpleNamespace

        pairing = self._pairing()
        return SimpleNamespace(
            skill_subset=list(pairing.skill_subset),
            target_subset=list(pairing.target_subset),
            subset_pairings=pairing,
        )

    def test_deploy_overlay_adds_skill_to_every_selected_target_only(self):
        from types import SimpleNamespace

        from apm_cli.install.package_resolution import effective_dependency_selection

        ctx = SimpleNamespace(skill_subset=("delta",), skill_subset_from_cli=True)
        selection = effective_dependency_selection(self._dep(), ctx)
        assert selection.skill_subset == ("alpha", "delta", "gamma")
        assert selection.skill_pairing.skills_for_target("claude") == ("alpha", "delta")
        assert selection.skill_pairing.skills_for_target("copilot") == ("delta", "gamma")
        assert not selection.skill_pairing.selects_target("cursor")

    def test_star_overlay_drops_pairing_and_deploys_all(self):
        from types import SimpleNamespace

        from apm_cli.install.package_resolution import effective_dependency_selection

        ctx = SimpleNamespace(skill_subset=(), skill_subset_from_cli=True)
        selection = effective_dependency_selection(self._dep(), ctx)
        assert selection.skill_subset is None
        assert selection.skill_pairing is None

    def test_lockfile_overlay_records_the_same_widened_pairing(self):
        from types import SimpleNamespace

        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.install.phases.lockfile import LockfileBuilder

        pairing = self._pairing()
        entry = LockedDependency(
            repo_url="owner/bundle",
            package_type="skill_bundle",
            skill_subset=list(pairing.skill_subset),
            target_subset=list(pairing.target_subset),
            subset_pairings=pairing.to_lock_records(),
        )
        lockfile = LockFile(dependencies={entry.get_unique_key(): entry})
        ctx = SimpleNamespace(skill_subset=("delta",), skill_subset_from_cli=True)
        LockfileBuilder(ctx)._attach_skill_subset_override(lockfile)

        assert entry.skill_subset == ["alpha", "delta", "gamma"]
        assert entry.selection() == pairing.with_edge(["delta"], ["claude", "copilot"])
        assert entry.subset_pairings == [
            {"skills": ["alpha", "delta"], "targets": ["claude"]},
            {"skills": ["delta", "gamma"], "targets": ["copilot"]},
        ]
