"""Tests for BaseIntegrator shared infrastructure.

Covers collision detection, path validation, partition_managed_files,
cleanup_empty_parents, sync_remove_files, find_files_by_glob, and
the IntegrationResult dataclass.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.primitives.discovery import discover_primitives


# ---------------------------------------------------------------------------
# IntegrationResult
# ---------------------------------------------------------------------------

class TestIntegrationResult:
    def test_basic_construction(self):
        r = IntegrationResult(
            files_integrated=3,
            files_updated=0,
            files_skipped=1,
            target_paths=[Path("/tmp/a"), Path("/tmp/b")],
        )
        assert r.files_integrated == 3
        assert r.files_updated == 0
        assert r.files_skipped == 1
        assert len(r.target_paths) == 2

    def test_optional_fields_default_to_zero(self):
        r = IntegrationResult(
            files_integrated=0,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
        )
        assert r.links_resolved == 0
        assert r.scripts_copied == 0
        assert r.sub_skills_promoted == 0
        assert r.skill_created is False

    def test_optional_fields_can_be_set(self):
        r = IntegrationResult(
            files_integrated=1,
            files_updated=0,
            files_skipped=0,
            target_paths=[],
            links_resolved=2,
            scripts_copied=1,
            sub_skills_promoted=3,
            skill_created=True,
        )
        assert r.links_resolved == 2
        assert r.scripts_copied == 1
        assert r.sub_skills_promoted == 3
        assert r.skill_created is True


# ---------------------------------------------------------------------------
# check_collision
# ---------------------------------------------------------------------------

class TestCheckCollision:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_collision_managed_files_none(self):
        """When managed_files is None, no collision possible."""
        target = self.root / "file.md"
        target.write_text("content")
        assert BaseIntegrator.check_collision(target, "file.md", None, False) is False

    def test_no_collision_file_does_not_exist(self):
        """File doesn't exist -> no collision."""
        target = self.root / "nonexistent.md"
        managed = set()
        assert BaseIntegrator.check_collision(target, "file.md", managed, False) is False

    def test_no_collision_file_is_managed(self):
        """File exists but is in managed set -> not a collision."""
        target = self.root / "file.md"
        target.write_text("content")
        managed = {"file.md"}
        assert BaseIntegrator.check_collision(target, "file.md", managed, False) is False

    def test_collision_unmanaged_file_exists_no_force(self):
        """File exists, not in managed set, force=False -> collision."""
        target = self.root / "file.md"
        target.write_text("user content")
        managed = set()
        assert BaseIntegrator.check_collision(target, "file.md", managed, False) is True

    def test_no_collision_force_overrides(self):
        """force=True suppresses collision even for unmanaged files."""
        target = self.root / "file.md"
        target.write_text("user content")
        managed = set()
        assert BaseIntegrator.check_collision(target, "file.md", managed, True) is False

    def test_collision_records_to_diagnostics(self):
        """Collision with diagnostics arg records the skip."""
        target = self.root / "file.md"
        target.write_text("user content")
        managed = set()
        diag = MagicMock()
        result = BaseIntegrator.check_collision(target, "file.md", managed, False, diag)
        assert result is True
        diag.skip.assert_called_once_with("file.md")

    def test_collision_warns_without_diagnostics(self):
        """Collision without diagnostics emits a warning."""
        target = self.root / "file.md"
        target.write_text("user content")
        managed = set()
        with patch("apm_cli.integration.base_integrator._rich_warning") as mock_warn:
            result = BaseIntegrator.check_collision(target, "file.md", managed, False)
        assert result is True
        mock_warn.assert_called_once()

    def test_backslash_normalized_in_rel_path(self):
        """rel_path with backslashes is normalized before managed lookup."""
        target = self.root / "file.md"
        target.write_text("content")
        # Managed set uses forward slashes; rel_path uses backslash
        managed = {"sub/file.md"}
        assert BaseIntegrator.check_collision(
            target, "sub\\file.md", managed, False
        ) is False


# ---------------------------------------------------------------------------
# normalize_managed_files
# ---------------------------------------------------------------------------

class TestNormalizeManagedFiles:
    def test_none_returns_none(self):
        assert BaseIntegrator.normalize_managed_files(None) is None

    def test_empty_set(self):
        assert BaseIntegrator.normalize_managed_files(set()) == set()

    def test_forward_slashes_unchanged(self):
        mf = {".github/prompts/foo.md", ".claude/rules/bar.mdc"}
        assert BaseIntegrator.normalize_managed_files(mf) == mf

    def test_backslashes_converted(self):
        mf = {".github\\prompts\\foo.md"}
        result = BaseIntegrator.normalize_managed_files(mf)
        assert result == {".github/prompts/foo.md"}

    def test_mixed_slashes(self):
        mf = {".github\\prompts/foo.md", ".claude/rules\\bar.mdc"}
        result = BaseIntegrator.normalize_managed_files(mf)
        assert result == {".github/prompts/foo.md", ".claude/rules/bar.mdc"}


# ---------------------------------------------------------------------------
# validate_deploy_path
# ---------------------------------------------------------------------------

class TestValidateDeployPath:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_github_prompt_path(self):
        assert BaseIntegrator.validate_deploy_path(
            ".github/prompts/foo.prompt.md", self.root
        ) is True

    def test_valid_claude_rules_path(self):
        assert BaseIntegrator.validate_deploy_path(
            ".claude/rules/foo.mdc", self.root
        ) is True

    def test_traversal_rejected(self):
        assert BaseIntegrator.validate_deploy_path(
            "../evil.md", self.root
        ) is False

    def test_traversal_in_middle_rejected(self):
        assert BaseIntegrator.validate_deploy_path(
            ".github/../etc/passwd", self.root
        ) is False

    def test_unknown_prefix_rejected(self):
        assert BaseIntegrator.validate_deploy_path(
            "random/file.md", self.root
        ) is False

    def test_custom_allowed_prefixes(self):
        assert BaseIntegrator.validate_deploy_path(
            ".github/custom/file.md",
            self.root,
            allowed_prefixes=(".github/",),
        ) is True

    def test_custom_prefixes_rejects_unknown(self):
        assert BaseIntegrator.validate_deploy_path(
            ".claude/rules/file.md",
            self.root,
            allowed_prefixes=(".github/",),
        ) is False

    def test_agents_path_valid(self):
        assert BaseIntegrator.validate_deploy_path(
            ".agents/skills/foo/", self.root
        ) is True

    def test_codex_hooks_json_valid(self):
        assert BaseIntegrator.validate_deploy_path(
            ".codex/hooks.json", self.root
        ) is True


# ---------------------------------------------------------------------------
# partition_bucket_key
# ---------------------------------------------------------------------------

class TestPartitionBucketKey:
    def test_prompts_copilot_aliased(self):
        assert BaseIntegrator.partition_bucket_key("prompts", "copilot") == "prompts"

    def test_agents_copilot_aliased(self):
        assert BaseIntegrator.partition_bucket_key("agents", "copilot") == "agents_github"

    def test_instructions_copilot_aliased(self):
        assert BaseIntegrator.partition_bucket_key("instructions", "copilot") == "instructions"

    def test_instructions_cursor_aliased(self):
        assert BaseIntegrator.partition_bucket_key("instructions", "cursor") == "rules_cursor"

    def test_instructions_claude_aliased(self):
        assert BaseIntegrator.partition_bucket_key("instructions", "claude") == "rules_claude"

    def test_commands_claude_aliased(self):
        assert BaseIntegrator.partition_bucket_key("commands", "claude") == "commands"

    def test_no_alias_falls_through(self):
        assert BaseIntegrator.partition_bucket_key("agents", "claude") == "agents_claude"

    def test_no_alias_opencode(self):
        assert BaseIntegrator.partition_bucket_key("agents", "opencode") == "agents_opencode"


# ---------------------------------------------------------------------------
# partition_managed_files
# ---------------------------------------------------------------------------

class TestPartitionManagedFiles:
    def test_empty_set_returns_empty_buckets(self):
        result = BaseIntegrator.partition_managed_files(set())
        assert isinstance(result, dict)
        # All buckets present but empty
        assert result["skills"] == set()
        assert result["hooks"] == set()

    def test_prompt_goes_to_prompts_bucket(self):
        mf = {".github/prompts/foo.prompt.md"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".github/prompts/foo.prompt.md" in result["prompts"]

    def test_claude_rules_goes_to_rules_claude_bucket(self):
        mf = {".claude/rules/foo.mdc"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".claude/rules/foo.mdc" in result["rules_claude"]

    def test_cursor_rules_goes_to_rules_cursor_bucket(self):
        mf = {".cursor/rules/foo.mdc"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".cursor/rules/foo.mdc" in result["rules_cursor"]

    def test_opencode_agents_bucket(self):
        mf = {".opencode/agents/foo.md"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".opencode/agents/foo.md" in result["agents_opencode"]

    def test_skills_cross_target_bucket(self):
        mf = {".github/skills/my-skill/skill.md"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".github/skills/my-skill/skill.md" in result["skills"]

    def test_hooks_cross_target_bucket(self):
        mf = {".github/hooks/pre-tool-use.sh"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".github/hooks/pre-tool-use.sh" in result["hooks"]

    def test_codex_agents_bucket(self):
        mf = {".codex/agents/my-agent.toml"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".codex/agents/my-agent.toml" in result["agents_codex"]

    def test_agents_skills_go_to_skills_bucket(self):
        """Codex skills deploy under .agents/ (deploy_root override)."""
        mf = {".agents/skills/my-skill/skill.md"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".agents/skills/my-skill/skill.md" in result["skills"]

    def test_unrecognized_path_not_in_any_bucket(self):
        mf = {"random/unknown/path.md"}
        result = BaseIntegrator.partition_managed_files(mf)
        # Should not appear in any bucket
        all_vals = set()
        for v in result.values():
            all_vals.update(v)
        assert "random/unknown/path.md" not in all_vals

    def test_multiple_files_multiple_buckets(self):
        mf = {
            ".github/prompts/foo.prompt.md",
            ".claude/rules/bar.mdc",
            ".github/skills/my-skill/skill.md",
            ".github/hooks/pre-run.sh",
        }
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".github/prompts/foo.prompt.md" in result["prompts"]
        assert ".claude/rules/bar.mdc" in result["rules_claude"]
        assert ".github/skills/my-skill/skill.md" in result["skills"]
        assert ".github/hooks/pre-run.sh" in result["hooks"]

    def test_github_instructions_bucket(self):
        mf = {".github/instructions/foo.instructions.md"}
        result = BaseIntegrator.partition_managed_files(mf)
        assert ".github/instructions/foo.instructions.md" in result["instructions"]


# ---------------------------------------------------------------------------
# cleanup_empty_parents
# ---------------------------------------------------------------------------

class TestCleanupEmptyParents:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_removes_empty_parent(self):
        subdir = self.root / "a" / "b"
        subdir.mkdir(parents=True)
        deleted = [subdir / "file.md"]  # File already deleted; dir is now empty
        BaseIntegrator.cleanup_empty_parents(deleted, self.root)
        assert not (self.root / "a" / "b").exists()
        assert not (self.root / "a").exists()

    def test_does_not_remove_stop_at_dir(self):
        subdir = self.root / "a"
        subdir.mkdir()
        deleted = [subdir / "file.md"]
        BaseIntegrator.cleanup_empty_parents(deleted, self.root)
        # stop_at (root) should never be removed
        assert self.root.exists()

    def test_does_not_remove_non_empty_parent(self):
        subdir = self.root / "a" / "b"
        subdir.mkdir(parents=True)
        # Leave a sibling file in "a"
        (self.root / "a" / "sibling.md").write_text("keep me")
        deleted = [subdir / "file.md"]
        BaseIntegrator.cleanup_empty_parents(deleted, self.root)
        assert (self.root / "a").exists()  # Not empty -> kept

    def test_empty_deleted_list_is_noop(self):
        # Should not raise
        BaseIntegrator.cleanup_empty_parents([], self.root)

    def test_nested_cleanup(self):
        deep = self.root / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        deleted = [deep / "file.md"]
        BaseIntegrator.cleanup_empty_parents(deleted, self.root)
        assert not (self.root / "a").exists()

    def test_multiple_deleted_paths(self):
        dir1 = self.root / "x"
        dir2 = self.root / "y"
        dir1.mkdir()
        dir2.mkdir()
        deleted = [dir1 / "f1.md", dir2 / "f2.md"]
        BaseIntegrator.cleanup_empty_parents(deleted, self.root)
        assert not dir1.exists()
        assert not dir2.exists()


# ---------------------------------------------------------------------------
# sync_remove_files
# ---------------------------------------------------------------------------

class TestSyncRemoveFiles:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_file(self, rel_path: str) -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content")
        return p

    def test_removes_matching_managed_file(self):
        self._make_file(".github/prompts/foo.prompt.md")
        mf = {".github/prompts/foo.prompt.md"}
        stats = BaseIntegrator.sync_remove_files(
            self.root, mf, ".github/prompts/"
        )
        assert stats["files_removed"] == 1
        assert not (self.root / ".github/prompts/foo.prompt.md").exists()

    def test_skips_non_matching_prefix(self):
        self._make_file(".github/prompts/foo.prompt.md")
        mf = {".github/prompts/foo.prompt.md"}
        stats = BaseIntegrator.sync_remove_files(
            self.root, mf, ".claude/rules/"
        )
        assert stats["files_removed"] == 0
        assert (self.root / ".github/prompts/foo.prompt.md").exists()

    def test_removes_multiple_files(self):
        self._make_file(".github/prompts/a.prompt.md")
        self._make_file(".github/prompts/b.prompt.md")
        mf = {
            ".github/prompts/a.prompt.md",
            ".github/prompts/b.prompt.md",
        }
        stats = BaseIntegrator.sync_remove_files(
            self.root, mf, ".github/prompts/"
        )
        assert stats["files_removed"] == 2

    def test_skips_nonexistent_file(self):
        mf = {".github/prompts/missing.md"}
        stats = BaseIntegrator.sync_remove_files(
            self.root, mf, ".github/prompts/"
        )
        assert stats["files_removed"] == 0
        assert stats["errors"] == 0

    def test_legacy_glob_fallback_when_no_managed_files(self):
        prompts_dir = self.root / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "foo-apm.prompt.md").write_text("content")
        (prompts_dir / "bar-apm.prompt.md").write_text("content")
        (prompts_dir / "user-custom.md").write_text("keep")

        stats = BaseIntegrator.sync_remove_files(
            self.root,
            None,  # No managed_files -> legacy glob
            ".github/prompts/",
            legacy_glob_dir=prompts_dir,
            legacy_glob_pattern="*-apm.prompt.md",
        )
        assert stats["files_removed"] == 2
        assert (prompts_dir / "user-custom.md").exists()

    def test_managed_files_none_no_legacy_is_noop(self):
        stats = BaseIntegrator.sync_remove_files(
            self.root, None, ".github/prompts/"
        )
        assert stats["files_removed"] == 0
        assert stats["errors"] == 0

    def test_traversal_path_is_not_removed(self):
        """validate_deploy_path rejects paths with '..'."""
        evil = "../evil.md"
        mf = {evil}
        stats = BaseIntegrator.sync_remove_files(self.root, mf, "../")
        assert stats["files_removed"] == 0


# ---------------------------------------------------------------------------
# find_files_by_glob
# ---------------------------------------------------------------------------

class TestFindFilesByGlob:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_matching_files(self):
        (self.root / "foo.prompt.md").write_text("a")
        (self.root / "bar.prompt.md").write_text("b")
        (self.root / "other.txt").write_text("c")
        results = BaseIntegrator.find_files_by_glob(self.root, "*.prompt.md")
        names = {f.name for f in results}
        assert names == {"foo.prompt.md", "bar.prompt.md"}

    def test_searches_subdirs(self):
        subdir = self.root / ".apm" / "prompts"
        subdir.mkdir(parents=True)
        (subdir / "sub.prompt.md").write_text("content")
        results = BaseIntegrator.find_files_by_glob(
            self.root, "*.prompt.md", subdirs=[".apm/prompts"]
        )
        assert any(f.name == "sub.prompt.md" for f in results)

    def test_symlinks_excluded(self):
        real_file = self.root / "real.prompt.md"
        real_file.write_text("content")
        link = self.root / "link.prompt.md"
        try:
            link.symlink_to(real_file)
        except OSError:
            pytest.skip("symlinks are not supported in this test environment")
        results = BaseIntegrator.find_files_by_glob(self.root, "*.prompt.md")
        names = {f.name for f in results}
        assert "link.prompt.md" not in names
        assert "real.prompt.md" in names

    def test_empty_directory_returns_empty(self):
        results = BaseIntegrator.find_files_by_glob(self.root, "*.md")
        assert results == []

    def test_nonexistent_subdir_is_skipped(self):
        results = BaseIntegrator.find_files_by_glob(
            self.root, "*.md", subdirs=["nonexistent/subdir"]
        )
        assert results == []

    def test_deduplicates_results(self):
        """Same file found via root and subdir should appear once."""
        # Root contains a file; subdir IS the root -> same file discovered twice
        (self.root / "foo.md").write_text("content")
        results = BaseIntegrator.find_files_by_glob(
            self.root, "*.md", subdirs=["."]  # '.' resolves to same dir
        )
        names = [f.name for f in results]
        assert names.count("foo.md") == 1

    def test_returns_sorted_results(self):
        for name in ["c.prompt.md", "a.prompt.md", "b.prompt.md"]:
            (self.root / name).write_text("x")
        results = BaseIntegrator.find_files_by_glob(self.root, "*.prompt.md")
        names = [f.name for f in results]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# resolve_links
# ---------------------------------------------------------------------------

class TestResolveLinks:
    def test_no_resolver_returns_content_unchanged(self):
        bi = BaseIntegrator()
        content = "Hello [link](foo.md)"
        result, count = bi.resolve_links(content, Path("src.md"), Path("tgt.md"))
        assert result == content
        assert count == 0

    def test_resolver_no_changes_returns_zero(self):
        bi = BaseIntegrator()
        mock_resolver = MagicMock()
        mock_resolver.resolve_links_for_installation.return_value = "Hello [link](foo.md)"
        bi.link_resolver = mock_resolver
        content = "Hello [link](foo.md)"
        result, count = bi.resolve_links(content, Path("src.md"), Path("tgt.md"))
        assert count == 0

    def test_resolver_changes_links_counts_removed(self):
        bi = BaseIntegrator()
        mock_resolver = MagicMock()
        # Simulate resolver replacing one link
        mock_resolver.resolve_links_for_installation.return_value = "Hello [link](resolved.md)"
        bi.link_resolver = mock_resolver
        content = "Hello [link](original.md)"
        result, count = bi.resolve_links(content, Path("src.md"), Path("tgt.md"))
        assert result == "Hello [link](resolved.md)"
        assert count == 1  # 1 original link resolved away


# ---------------------------------------------------------------------------
# should_integrate
# ---------------------------------------------------------------------------

class TestShouldIntegrate:
    def test_always_returns_true(self):
        bi = BaseIntegrator()
        assert bi.should_integrate(Path("/any/path")) is True


# ---------------------------------------------------------------------------
# init_link_resolver — home-directory scoping (#830)
# ---------------------------------------------------------------------------

class TestInitLinkResolverHomeScoping:
    """When install_path is $HOME, init_link_resolver must scope
    discover_primitives to ~/.apm/ to avoid recursive-globbing the
    entire home directory.  See issue #830."""

    @patch("apm_cli.integration.base_integrator.discover_primitives")
    @patch("apm_cli.integration.base_integrator.UnifiedLinkResolver")
    def test_scopes_to_apm_subdir_when_install_path_is_home(
        self, mock_resolver_cls, mock_discover
    ):
        mock_discover.return_value = []
        bi = BaseIntegrator()
        pkg_info = MagicMock()
        pkg_info.install_path = Path.home()

        bi.init_link_resolver(pkg_info, Path.home())

        mock_discover.assert_called_once_with(Path.home() / ".apm")

    @patch("apm_cli.integration.base_integrator.discover_primitives")
    @patch("apm_cli.integration.base_integrator.UnifiedLinkResolver")
    def test_uses_install_path_when_not_home(
        self, mock_resolver_cls, mock_discover, tmp_path
    ):
        mock_discover.return_value = []
        bi = BaseIntegrator()
        pkg_info = MagicMock()
        pkg_info.install_path = tmp_path

        bi.init_link_resolver(pkg_info, tmp_path)

        mock_discover.assert_called_once_with(tmp_path)
