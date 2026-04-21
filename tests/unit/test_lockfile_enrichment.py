"""Unit tests for apm_cli.bundle.lockfile_enrichment."""

import yaml

from apm_cli.bundle.lockfile_enrichment import enrich_lockfile_for_pack
from apm_cli.deps.lockfile import LockFile, LockedDependency


def _make_lockfile() -> LockFile:
    """Create a simple lockfile with one dependency."""
    lf = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        resolved_commit="abc123",
        version="1.0.0",
        deployed_files=[".github/agents/a.md"],
    )
    lf.add_dependency(dep)
    return lf


class TestLockfileEnrichment:
    def test_adds_pack_section(self):
        lf = _make_lockfile()
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="vscode")
        parsed = yaml.safe_load(result)

        assert "pack" in parsed
        assert parsed["pack"]["format"] == "apm"
        assert parsed["pack"]["target"] == "vscode"
        assert "packed_at" in parsed["pack"]

    def test_preserves_dependencies(self):
        lf = _make_lockfile()
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="all")
        parsed = yaml.safe_load(result)

        assert "dependencies" in parsed
        assert len(parsed["dependencies"]) == 1
        assert parsed["dependencies"][0]["repo_url"] == "owner/repo"
        assert parsed["dependencies"][0]["resolved_commit"] == "abc123"

    def test_preserves_lockfile_version(self):
        lf = _make_lockfile()
        result = enrich_lockfile_for_pack(lf, fmt="plugin", target="claude")
        parsed = yaml.safe_load(result)

        assert parsed["lockfile_version"] == "1"

    def test_does_not_mutate_original(self):
        lf = _make_lockfile()
        original_yaml = lf.to_yaml()

        enrich_lockfile_for_pack(lf, fmt="apm", target="all")

        assert lf.to_yaml() == original_yaml

    def test_filters_deployed_files_by_target(self):
        """Pack with --target copilot should exclude .claude/ files from lockfile."""
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[
                ".github/agents/a.md",
                ".github/skills/s1",
                ".claude/commands/c.md",
                ".claude/skills/review",
            ],
        )
        lf.add_dependency(dep)

        result = enrich_lockfile_for_pack(lf, fmt="apm", target="copilot")
        parsed = yaml.safe_load(result)

        deployed = parsed["dependencies"][0]["deployed_files"]
        assert ".github/agents/a.md" in deployed
        assert ".github/skills/s1" in deployed
        assert ".claude/commands/c.md" not in deployed
        assert ".claude/skills/review" not in deployed

    def test_filters_deployed_files_target_all_keeps_everything(self):
        """Pack with --target all should keep all deployed files."""
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[
                ".github/agents/a.md",
                ".claude/commands/c.md",
            ],
        )
        lf.add_dependency(dep)

        result = enrich_lockfile_for_pack(lf, fmt="apm", target="all")
        parsed = yaml.safe_load(result)

        deployed = parsed["dependencies"][0]["deployed_files"]
        assert len(deployed) == 2

    def test_cross_target_mapping_github_to_claude(self):
        """Skills under .github/ should be remapped to .claude/ in enriched lockfile."""
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[
                ".github/skills/my-plugin/",
                ".github/skills/my-plugin/SKILL.md",
            ],
        )
        lf.add_dependency(dep)

        result = enrich_lockfile_for_pack(lf, fmt="apm", target="claude")
        parsed = yaml.safe_load(result)

        deployed = parsed["dependencies"][0]["deployed_files"]
        assert ".claude/skills/my-plugin/" in deployed
        assert ".claude/skills/my-plugin/SKILL.md" in deployed
        assert all(f.startswith(".claude/") for f in deployed)

    def test_cross_target_mapping_records_mapped_from(self):
        """When mapping occurs, pack section records mapped_from."""
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[".github/skills/x/SKILL.md"],
        )
        lf.add_dependency(dep)

        result = enrich_lockfile_for_pack(lf, fmt="apm", target="claude")
        parsed = yaml.safe_load(result)

        assert "mapped_from" in parsed["pack"]
        assert ".github/skills/" in parsed["pack"]["mapped_from"]

    def test_no_mapped_from_when_no_mapping(self):
        """When no mapping occurs, pack section should not have mapped_from."""
        lf = _make_lockfile()
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="vscode")
        parsed = yaml.safe_load(result)

        assert "mapped_from" not in parsed["pack"]

    def test_cross_target_commands_not_mapped(self):
        """Commands should NOT be cross-mapped -- they are target-specific."""
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[
                ".github/commands/run.md",
                ".github/skills/x/SKILL.md",
            ],
        )
        lf.add_dependency(dep)

        result = enrich_lockfile_for_pack(lf, fmt="apm", target="claude")
        parsed = yaml.safe_load(result)

        deployed = parsed["dependencies"][0]["deployed_files"]
        # Skills mapped, commands dropped
        assert ".claude/skills/x/SKILL.md" in deployed
        assert ".github/commands/run.md" not in deployed
        assert ".claude/commands/run.md" not in deployed

    def test_copilot_alias_equivalent_to_vscode(self):
        """'copilot' target should produce the same enriched lockfile as 'vscode' (deprecated alias)."""
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[
                ".claude/skills/x/SKILL.md",
                ".claude/agents/a.md",
            ],
        )
        lf.add_dependency(dep)

        result_vscode = enrich_lockfile_for_pack(lf, fmt="apm", target="vscode")
        result_copilot = enrich_lockfile_for_pack(lf, fmt="apm", target="copilot")

        parsed_vscode = yaml.safe_load(result_vscode)
        parsed_copilot = yaml.safe_load(result_copilot)

        # Deployed files should be identical (both remap .claude/ -> .github/)
        assert (
            parsed_vscode["dependencies"][0]["deployed_files"]
            == parsed_copilot["dependencies"][0]["deployed_files"]
        )


class TestFilterFilesByTarget:
    """Direct tests for _filter_files_by_target."""

    def test_direct_match_no_mapping(self):
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/skills/x/SKILL.md"]
        filtered, mappings = _filter_files_by_target(files, "vscode")
        assert filtered == [".github/skills/x/SKILL.md"]
        assert mappings == {}

    def test_cross_map_github_to_claude(self):
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/skills/x/SKILL.md", ".github/agents/a.md"]
        filtered, mappings = _filter_files_by_target(files, "claude")
        assert ".claude/skills/x/SKILL.md" in filtered
        assert ".claude/agents/a.md" in filtered
        assert mappings[".claude/skills/x/SKILL.md"] == ".github/skills/x/SKILL.md"

    def test_dedup_direct_over_mapped(self):
        """If a file exists under both .github/ and .claude/, direct wins."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [
            ".claude/skills/x/SKILL.md",
            ".github/skills/x/SKILL.md",
        ]
        filtered, mappings = _filter_files_by_target(files, "claude")
        assert filtered.count(".claude/skills/x/SKILL.md") == 1
        # The direct match should NOT appear in mappings
        assert ".claude/skills/x/SKILL.md" not in mappings

    def test_traversal_path_not_escaped(self):
        """Mapping must not allow path components to escape target prefix."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        # A crafted file path with traversal should only remap the prefix,
        # the traversal components remain as literal path segments
        files = [".github/skills/../../etc/passwd"]
        filtered, mappings = _filter_files_by_target(files, "claude")
        # The mapping still happens (prefix replacement) but the packer's
        # bundle-escape check will catch the bad destination path
        if filtered:
            for f in filtered:
                assert f.startswith(".claude/skills/")
        # Either way, the original .github/ path should not sneak through
        assert ".github/skills/../../etc/passwd" not in filtered


class TestFilterFilesByTargetList:
    """Tests for _filter_files_by_target with list targets."""

    def test_list_claude_copilot_includes_both_prefixes(self):
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/agents/a.md", ".claude/commands/b.md", ".cursor/rules/r.md"]
        filtered, mappings = _filter_files_by_target(files, ["claude", "vscode"])
        assert ".github/agents/a.md" in filtered
        assert ".claude/commands/b.md" in filtered
        # .cursor/ is not in ["claude", "vscode"] prefixes
        assert ".cursor/rules/r.md" not in filtered
        # Both are direct matches under their respective prefixes, no mapping needed
        assert mappings == {}

    def test_list_single_element_same_as_string(self):
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/skills/x/SKILL.md", ".claude/commands/b.md"]
        filtered_list, maps_list = _filter_files_by_target(files, ["claude"])
        filtered_str, maps_str = _filter_files_by_target(files, "claude")
        assert filtered_list == filtered_str
        assert maps_list == maps_str

    def test_list_claude_cursor_includes_both(self):
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".claude/skills/s1/SKILL.md", ".cursor/rules/r.md", ".github/agents/a.md"]
        filtered, mappings = _filter_files_by_target(files, ["claude", "cursor"])
        assert ".claude/skills/s1/SKILL.md" in filtered
        assert ".cursor/rules/r.md" in filtered
        # .github/ is not a direct prefix for either claude or cursor
        # but cross-target maps may apply
        assert ".github/agents/a.md" not in filtered

    def test_list_deduplicates_prefixes(self):
        """copilot and vscode share the same prefix .github/ -- no duplicates."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/agents/a.md"]
        filtered, mappings = _filter_files_by_target(files, ["copilot", "vscode"])
        assert filtered == [".github/agents/a.md"]
        assert mappings == {}

    def test_list_cross_map_github_to_claude_and_cursor(self):
        """When both claude and cursor are targets, cross-mapped files go to one dest."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/skills/x/SKILL.md"]
        filtered, mappings = _filter_files_by_target(files, ["claude", "cursor"])
        # Both claude and cursor have cross-maps from .github/skills/
        # Dict.update means cursor map overwrites claude map for same key
        # So the result maps to cursor's destination
        assert len(filtered) == 1
        assert len(mappings) == 1


class TestEnrichLockfileListTarget:
    """Tests for enrich_lockfile_for_pack with list targets."""

    def test_list_target_serializes_as_comma_string(self):
        lf = _make_lockfile()
        result = enrich_lockfile_for_pack(lf, fmt="apm", target=["claude", "vscode"])
        parsed = yaml.safe_load(result)

        assert parsed["pack"]["target"] == "claude,vscode"

    def test_list_target_filters_deployed_files(self):
        lf = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="abc123",
            version="1.0.0",
            deployed_files=[
                ".github/agents/a.md",
                ".claude/commands/c.md",
                ".cursor/rules/r.md",
            ],
        )
        lf.add_dependency(dep)

        result = enrich_lockfile_for_pack(lf, fmt="apm", target=["claude", "vscode"])
        parsed = yaml.safe_load(result)

        deployed = parsed["dependencies"][0]["deployed_files"]
        assert ".github/agents/a.md" in deployed
        assert ".claude/commands/c.md" in deployed
        # .cursor/ not in target list
        assert ".cursor/rules/r.md" not in deployed

    def test_list_target_single_element_equivalent_to_string(self):
        lf = _make_lockfile()
        result_list = enrich_lockfile_for_pack(lf, fmt="apm", target=["vscode"])
        result_str = enrich_lockfile_for_pack(lf, fmt="apm", target="vscode")

        parsed_list = yaml.safe_load(result_list)
        parsed_str = yaml.safe_load(result_str)

        # Deployed files should be identical
        assert (
            parsed_list["dependencies"][0]["deployed_files"]
            == parsed_str["dependencies"][0]["deployed_files"]
        )
