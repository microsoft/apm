"""Unit tests for Phase 11: skill subset persistence (filesystem, process and CLI boundary cases)."""

import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-marketplace-locked-selection", "P-OWN-frontmatter-bom-bounded-yaml"),
]


# ============================================================================
# DependencyReference — parse_from_dict with skills: field
# ============================================================================


# ============================================================================
# LockedDependency — skill_subset round-trip
# ============================================================================


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
        """Use the real lookup owner so fixtures follow the lockfile contract."""
        return LockFile(dependencies=locked_deps)

    def _make_dep_ref(self, repo_url, skill_subset=None):
        ref = DependencyReference.parse(repo_url)
        ref.skill_subset = skill_subset
        return ref

    def _make_locked_dep(self, package_type="skill_bundle", skill_subset=None):
        return LockedDependency(
            repo_url="owner/repo",
            package_type=package_type,
            skill_subset=skill_subset or [],
        )

    def _package_with_skills(self, tmp_path: Path, *names: str) -> Path:
        package = tmp_path / "apm_modules" / "owner" / "repo"
        for name in names:
            skill = package / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        return package

    def test_consistent_passes(self, tmp_path: Path):
        """Matching skill_subset → check passes."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha", "beta"])
        self._package_with_skills(tmp_path, "alpha", "beta")
        locked = self._make_locked_dep(skill_subset=["alpha", "beta"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is True

    def test_mismatch_fails(self, tmp_path: Path):
        """Different skill_subset → check fails."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha", "beta"])
        self._package_with_skills(tmp_path, "alpha", "beta")
        locked = self._make_locked_dep(skill_subset=["alpha"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is False
        assert "mismatch" in result.message

    def test_no_manifest_subset_vs_lock_subset_fails(self, tmp_path: Path):
        """Manifest has no skills: but lockfile has skill_subset → fails."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=None)
        self._package_with_skills(tmp_path, "alpha")
        locked = self._make_locked_dep(skill_subset=["alpha"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is False

    def test_marketplace_plugin_subset_mismatch_fails(self, tmp_path: Path):
        """Plugin package subsets are checked because CLI --skill supports them."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha"])
        self._package_with_skills(tmp_path, "alpha")
        locked = self._make_locked_dep(package_type="marketplace_plugin", skill_subset=[])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is False

    def test_missing_from_lock_skipped(self, tmp_path: Path):
        """Deps not in lockfile are skipped (other checks catch this)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=["alpha"])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({})

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is True

    def test_both_empty_passes(self, tmp_path: Path):
        """Both manifest and lockfile with no skill_subset → passes."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        dep_ref = self._make_dep_ref("owner/repo", skill_subset=None)
        self._package_with_skills(tmp_path, "alpha")
        locked = self._make_locked_dep(skill_subset=[])
        manifest = self._make_manifest_mock([dep_ref])
        lock = self._make_lock_mock({"owner/repo": locked})

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is True

    def test_transitive_union_passes_and_missing_manifest_is_unverified(self, tmp_path: Path):
        """A root plus transitive declaration audits as their union (#3735)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        self._package_with_skills(tmp_path, "alpha", "gamma")
        parent = tmp_path / "apm_modules" / "owner" / "parent"
        parent.mkdir(parents=True)
        (parent / "apm.yml").write_text(
            "name: parent\nversion: 1.0.0\ndependencies:\n  apm:\n"
            "    - git: https://github.com/owner/repo.git\n      skills: [gamma]\n",
            encoding="utf-8",
        )
        manifest = self._make_manifest_mock(
            [
                self._make_dep_ref("owner/repo", skill_subset=["alpha"]),
                self._make_dep_ref("owner/parent"),
            ]
        )
        lock = self._make_lock_mock(
            {
                "owner/repo": self._make_locked_dep(skill_subset=["alpha", "gamma"]),
                "owner/parent": LockedDependency(repo_url="owner/parent"),
            }
        )

        assert _check_skill_subset_consistency(manifest, lock, tmp_path).passed is True

        (parent / "apm.yml").unlink()
        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is False
        assert any("unverified" in detail for detail in result.details)

    def test_transitive_target_subset_mismatch_fails(self, tmp_path: Path):
        """A locked target_subset that disagrees with the declared merge fails (#3735)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        self._package_with_skills(tmp_path, "alpha")
        parent = tmp_path / "apm_modules" / "owner" / "parent"
        parent.mkdir(parents=True)
        (parent / "apm.yml").write_text(
            "name: parent\nversion: 1.0.0\ndependencies:\n  apm:\n"
            "    - git: https://github.com/owner/repo.git\n      targets: [claude]\n",
            encoding="utf-8",
        )
        manifest = self._make_manifest_mock([self._make_dep_ref("owner/parent")])
        bundle = self._make_locked_dep()
        bundle.target_subset = ["claude", "copilot"]
        lock = self._make_lock_mock(
            {"owner/repo": bundle, "owner/parent": LockedDependency(repo_url="owner/parent")}
        )

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is False
        assert result.details == [
            "owner/repo: declared targets ['claude'] != lockfile target_subset "
            "['claude', 'copilot']"
        ]

    def _parent_declaring(self, tmp_path: Path, declaration: str) -> Path:
        parent = tmp_path / "apm_modules" / "owner" / "parent"
        parent.mkdir(parents=True)
        (parent / "apm.yml").write_text(
            "name: parent\nversion: 1.0.0\ndependencies:\n  apm:\n" + declaration,
            encoding="utf-8",
        )
        return parent

    @pytest.mark.parametrize(
        "root_skills",
        [None, ["alpha"], "undeclared"],
        ids=["absorbed", "narrowed", "only-unreadable-parent"],
    )
    def test_unreadable_declaration_is_conclusive_only_when_absorbed(
        self, tmp_path: Path, root_skills
    ):
        """An incomplete walk proves "all" only through an unrestricted edge (#3735)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        self._package_with_skills(tmp_path, "alpha", "beta")
        self._parent_declaring(tmp_path, "    - broken: [\n")
        declared = [self._make_dep_ref("owner/parent")]
        if root_skills != "undeclared":
            declared.insert(0, self._make_dep_ref("owner/repo", skill_subset=root_skills))
        manifest = self._make_manifest_mock(declared)
        lock = self._make_lock_mock(
            {
                "owner/repo": self._make_locked_dep(
                    skill_subset=None if root_skills == "undeclared" else root_skills
                ),
                "owner/parent": LockedDependency(repo_url="owner/parent"),
            }
        )

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        if root_skills is None:
            assert result.passed is True
        else:
            assert result.details == [
                "owner/repo: selection unverified -- the manifests that declare it "
                "could not all be read"
            ]

    def test_transitive_marketplace_declaration_maps_to_its_locked_entry(self, tmp_path: Path):
        """A parent's marketplace declaration reaches the locked package (#3735)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        self._package_with_skills(tmp_path, "alpha", "beta")
        self._parent_declaring(tmp_path, "    - name: tools\n      marketplace: mkt\n")
        locked = self._make_locked_dep()
        locked.discovered_via = "mkt"
        locked.marketplace_plugin_name = "tools"
        manifest = self._make_manifest_mock(
            [
                self._make_dep_ref("owner/repo", skill_subset=["alpha"]),
                self._make_dep_ref("owner/parent"),
            ]
        )
        lock = self._make_lock_mock(
            {"owner/repo": locked, "owner/parent": LockedDependency(repo_url="owner/parent")}
        )

        # The parent's unrestricted marketplace edge absorbs the root's [alpha].
        assert _check_skill_subset_consistency(manifest, lock, tmp_path).passed is True

    def test_aliased_diamond_reads_the_locked_alias_location(self, tmp_path: Path):
        """An edge with another alias still finds the node where the lock put it (#3735)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        for name in ("alpha", "beta"):
            skill = tmp_path / "apm_modules" / "bun-a" / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        declared = DependencyReference.parse_from_dict(
            {"git": "owner/repo", "alias": "bun-b", "skills": ["alpha"]}
        )
        locked = self._make_locked_dep(skill_subset=["alpha"])
        locked.alias = "bun-a"
        manifest = self._make_manifest_mock([declared])

        result = _check_skill_subset_consistency(
            manifest, self._make_lock_mock({"owner/repo": locked}), tmp_path
        )
        assert result.passed is True, result.details

    def test_remote_parent_same_repo_path_child_is_declared(self, tmp_path: Path):
        """A fetched package's ``path:`` child expands as install expands it (#3735)."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        parent = tmp_path / "apm_modules" / "owner" / "repo"
        child = parent / "packages" / "dep"
        (child / "skills" / "alpha").mkdir(parents=True)
        (child / "skills" / "alpha" / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
        (parent / "apm.yml").write_text(
            "name: repo\nversion: 1.0.0\ndependencies:\n  apm:\n"
            "    - path: ./packages/dep\n      skills: [alpha]\n",
            encoding="utf-8",
        )
        manifest = self._make_manifest_mock([self._make_dep_ref("owner/repo")])
        nested = LockedDependency(
            repo_url="owner/repo",
            virtual_path="packages/dep",
            is_virtual=True,
            package_type="skill_bundle",
            skill_subset=["alpha"],
        )
        lock = LockFile(
            dependencies={
                "owner/repo": LockedDependency(repo_url="owner/repo"),
                nested.get_unique_key(): nested,
            }
        )

        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.passed is True, result.details

    def test_marketplace_declaration_audits_under_its_locked_key(self, tmp_path: Path):
        """A marketplace declaration's selection is checked against its locked entry."""
        from apm_cli.policy.ci_checks import _check_skill_subset_consistency

        self._package_with_skills(tmp_path, "alpha", "beta")
        declared = DependencyReference(
            repo_url="_marketplace/mkt/tools",
            is_marketplace=True,
            marketplace_name="mkt",
            marketplace_plugin_name="tools",
            skill_subset=["alpha"],
        )
        locked = self._make_locked_dep(skill_subset=["alpha"])
        locked.discovered_via = "mkt"
        locked.marketplace_plugin_name = "tools"
        manifest = self._make_manifest_mock([declared])

        lock = self._make_lock_mock({"owner/repo": locked})
        assert _check_skill_subset_consistency(manifest, lock, tmp_path).passed is True

        locked.skill_subset = ["alpha", "beta"]
        result = _check_skill_subset_consistency(manifest, lock, tmp_path)
        assert result.details == [
            "owner/repo: declared skills ['alpha'] != lockfile skill_subset ['alpha', 'beta']"
        ]


# ============================================================================
# get_existing_skill_subset — read persisted skills: from current_deps
# ============================================================================


# ============================================================================
# _resolve_package_references — additive --skill merge (issue #1771)
# ============================================================================


# ============================================================================
# _resolve_package_references -- registry identity must not silently convert
# to git form (regression: PR #2166 review)
# ============================================================================


# ---------------------------------------------------------------------------
# Unit: --skill overlay on an exact skills x targets pairing (#3763)
# ---------------------------------------------------------------------------
