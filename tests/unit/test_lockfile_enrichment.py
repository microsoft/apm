"""Unit tests for apm_cli.bundle.lockfile_enrichment."""

import pytest
import yaml

from apm_cli.bundle.lockfile_enrichment import enrich_lockfile_for_pack
from apm_cli.deps.lockfile import LockedDependency, LockFile


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
        filtered, mappings = _filter_files_by_target(files, "claude")  # noqa: RUF059
        # The mapping still happens (prefix replacement) but the packer's
        # bundle-escape check will catch the bad destination path
        if filtered:
            for f in filtered:
                assert f.startswith(".claude/skills/")
        # Either way, the original .github/ path should not sneak through
        assert ".github/skills/../../etc/passwd" not in filtered

    # -- agent-skills target (#737) ---------------------------------------

    def test_filter_files_agent_skills_target(self):
        """agent-skills returns .agents/skills entries directly and remaps
        .github/skills/ -> .agents/skills/.  Other prefixes (.github/agents/)
        are NOT included -- agent-skills is skills-only."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [
            ".agents/skills/x/SKILL.md",
            ".github/agents/a.md",
            ".github/skills/y/SKILL.md",
        ]
        filtered, mappings = _filter_files_by_target(files, "agent-skills")

        # Direct match on the .agents/ prefix.
        assert ".agents/skills/x/SKILL.md" in filtered
        # Cross-target remap from .github/skills/ -> .agents/skills/.
        assert ".agents/skills/y/SKILL.md" in filtered
        assert mappings[".agents/skills/y/SKILL.md"] == ".github/skills/y/SKILL.md"
        # .github/agents/ is NOT remapped -- agent-skills has no agents primitive.
        assert ".github/agents/a.md" not in filtered
        assert ".agents/agents/a.md" not in filtered
        # Every surviving entry lives under the agent-skills prefix.
        for f in filtered:
            assert f.startswith(".agents/")

    def test_filter_files_agent_skills_remap_escape_rejected(self):
        """A crafted .github/skills/ path with traversal cannot escape the
        .agents/ prefix when remapped for the agent-skills target."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        files = [".github/skills/../../etc/passwd"]
        filtered, mappings = _filter_files_by_target(files, "agent-skills")  # noqa: RUF059

        # Original .github/ path must never appear directly.
        assert ".github/skills/../../etc/passwd" not in filtered
        # The containment guard must reject traversal -- either the entry is
        # dropped entirely or every surviving path is well-formed.
        for f in filtered:
            assert f.startswith(".agents/")
            assert ".." not in f.split("/"), (
                f"traversal segment leaked through containment guard: {f}"
            )

    @pytest.mark.parametrize(
        "payload",
        [
            ".github/skills/x/../../etc/passwd",
            ".github/skills/../foo/SKILL.md",
            ".github/skills/x/./../../y/SKILL.md",
        ],
        ids=["double-dot-escape", "parent-traverse", "dot-mixed-traverse"],
    )
    def test_filter_files_agent_skills_traversal_payloads_rejected(self, payload: str):
        """Parametrized traversal payloads must be rejected or normalised safely."""
        from apm_cli.bundle.lockfile_enrichment import _filter_files_by_target

        filtered, _mappings = _filter_files_by_target([payload], "agent-skills")

        # Either the entry was rejected entirely …
        if not filtered:
            return
        # … or every surviving path is well-formed (no '..' segments).
        for f in filtered:
            parts = f.split("/")
            assert ".." not in parts, f"traversal segment leaked for payload {payload!r}: {f}"


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
        filtered, mappings = _filter_files_by_target(files, ["claude", "cursor"])  # noqa: RUF059
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


class TestWindsurfTargetParity:
    """Regression: --target windsurf must filter and cross-map correctly.

    Before the targets-registry refactor, ``_TARGET_PREFIXES`` and
    ``_CROSS_TARGET_MAPS`` both omitted ``"windsurf"``, so
    ``apm pack --target windsurf`` silently dropped every ``.windsurf/``
    file from the bundle lockfile.
    """

    def _lockfile_with(self, files: list[str]) -> LockFile:
        lf = LockFile()
        lf.add_dependency(
            LockedDependency(
                repo_url="owner/repo",
                resolved_commit="abc123",
                version="1.0.0",
                deployed_files=files,
            )
        )
        return lf

    def test_windsurf_target_includes_windsurf_prefix(self):
        lf = self._lockfile_with(
            [
                ".windsurf/skills/x/SKILL.md",
                ".unrelated/foo",
            ]
        )
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="windsurf")
        deployed = yaml.safe_load(result)["dependencies"][0]["deployed_files"]
        assert ".windsurf/skills/x/SKILL.md" in deployed
        assert ".unrelated/foo" not in deployed

    def test_windsurf_cross_map_skills_from_github(self):
        """``.github/skills/`` files are remapped under ``.windsurf/skills/``."""
        lf = self._lockfile_with([".github/skills/x/SKILL.md"])
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="windsurf")
        deployed = yaml.safe_load(result)["dependencies"][0]["deployed_files"]
        assert ".windsurf/skills/x/SKILL.md" in deployed

    def test_windsurf_cross_map_agents_collapse_to_skills(self):
        """``.github/agents/`` is intentionally remapped to ``.windsurf/skills/``
        because windsurf has no native agent surface (lossy conversion).
        """
        lf = self._lockfile_with([".github/agents/a.md"])
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="windsurf")
        deployed = yaml.safe_load(result)["dependencies"][0]["deployed_files"]
        assert ".windsurf/skills/a.md" in deployed

    def test_target_all_includes_windsurf_files(self):
        """``--target all`` must include ``.windsurf/`` files (was missing pre-refactor)."""
        lf = self._lockfile_with(
            [
                ".windsurf/skills/x/SKILL.md",
                ".github/agents/a.md",
                ".gemini/extensions/GEMINI.md",
            ]
        )
        result = enrich_lockfile_for_pack(lf, fmt="apm", target="all")
        deployed = yaml.safe_load(result)["dependencies"][0]["deployed_files"]
        assert ".windsurf/skills/x/SKILL.md" in deployed
        assert ".github/agents/a.md" in deployed
        # Gemini was also missing from the legacy "all" list -- registry derivation fixes that
        assert ".gemini/extensions/GEMINI.md" in deployed

    def test_multi_target_windsurf_plus_claude(self):
        lf = self._lockfile_with(
            [
                ".windsurf/skills/x/SKILL.md",
                ".claude/commands/c.md",
                ".cursor/rules/r.md",
            ]
        )
        result = enrich_lockfile_for_pack(lf, fmt="apm", target=["windsurf", "claude"])
        deployed = yaml.safe_load(result)["dependencies"][0]["deployed_files"]
        assert ".windsurf/skills/x/SKILL.md" in deployed
        assert ".claude/commands/c.md" in deployed
        assert ".cursor/rules/r.md" not in deployed

    def test_existing_targets_unchanged(self):
        """Regression: every legacy single-target prefix still works."""
        cases = [
            ("copilot", ".github/agents/a.md"),
            ("claude", ".claude/commands/c.md"),
            ("cursor", ".cursor/rules/r.md"),
            ("opencode", ".opencode/agents/a.md"),
            ("codex", ".codex/agents/a.md"),
            ("agent-skills", ".agents/skills/x/SKILL.md"),
        ]
        for target, path in cases:
            lf = self._lockfile_with([path, ".unrelated/foo"])
            result = enrich_lockfile_for_pack(lf, fmt="apm", target=target)
            deployed = yaml.safe_load(result)["dependencies"][0]["deployed_files"]
            assert path in deployed, f"{target}: {path} dropped after refactor"
            assert ".unrelated/foo" not in deployed, f"{target}: leaked unrelated file"

    def test_target_all_includes_every_deployable_target_prefix(self):
        """Structural guard: ``--target all`` must include the prefixes for
        every deployable target in KNOWN_TARGETS, not a hard-coded subset.

        This is the general-pattern guard for the silent-drop class of
        bug that originally hid ``.windsurf/`` and ``.gemini/`` from
        ``--target all``.  Adding a new deployable target (one with
        ``detect_by_dir or auto_create``) automatically extends this
        assertion -- if a future target's prefix is not picked up by
        ``_all_target_prefixes()``, this test fails immediately at
        registration time rather than silently in user output.
        """
        from apm_cli.bundle.lockfile_enrichment import _all_target_prefixes
        from apm_cli.integration.targets import KNOWN_TARGETS

        all_prefixes = _all_target_prefixes()
        for name, profile in KNOWN_TARGETS.items():
            if not (profile.detect_by_dir or profile.auto_create):
                continue
            for expected in profile.effective_pack_prefixes:
                assert expected in all_prefixes, (
                    f"target {name!r} prefix {expected!r} missing from "
                    f"_all_target_prefixes(); --target all would silently drop "
                    f"its files"
                )
