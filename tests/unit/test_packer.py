"""Unit tests for apm_cli.bundle.packer."""

import os
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from apm_cli.bundle.packer import pack_bundle, PackResult, _filter_files_by_target
from apm_cli.deps.lockfile import LockFile, LockedDependency


def _setup_project(tmp_path: Path, deployed_files: list[str], *, target: str | None = None) -> Path:
    """Create a minimal project with apm.yml, apm.lock.yaml, and deployed files on disk."""
    project = tmp_path / "project"
    project.mkdir()

    # apm.yml
    apm_yml = {"name": "test-pkg", "version": "1.0.0"}
    if target:
        apm_yml["target"] = target
    (project / "apm.yml").write_text(yaml.dump(apm_yml), encoding="utf-8")

    # Create deployed files on disk
    for fpath in deployed_files:
        full = project / fpath
        if fpath.endswith("/"):
            full.mkdir(parents=True, exist_ok=True)
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(f"content of {fpath}", encoding="utf-8")

    # apm.lock.yaml with a single dependency containing those files
    lockfile = LockFile()
    dep = LockedDependency(
        repo_url="owner/repo",
        resolved_commit="abc123",
        deployed_files=deployed_files,
    )
    lockfile.add_dependency(dep)
    lockfile.write(project / "apm.lock.yaml")

    return project


class TestFilterFilesByTarget:
    def test_copilot_only(self):
        files = [".github/agents/a.md", ".claude/commands/b.md"]
        result, mappings = _filter_files_by_target(files, "copilot")
        assert result == [".github/agents/a.md"]
        assert mappings == {}

    def test_claude_only(self):
        files = [".github/agents/a.md", ".claude/commands/b.md"]
        result, mappings = _filter_files_by_target(files, "claude")
        # .claude/commands/b.md is a direct match; .github/agents/a.md is
        # cross-mapped to .claude/agents/a.md (agents are target-equivalent).
        # Commands are target-specific and are NOT mapped.
        assert ".claude/commands/b.md" in result
        assert ".claude/agents/a.md" in result
        assert mappings == {".claude/agents/a.md": ".github/agents/a.md"}

    def test_all_includes_both(self):
        files = [".github/agents/a.md", ".claude/commands/b.md"]
        result, mappings = _filter_files_by_target(files, "all")
        assert result == files
        assert mappings == {}


class TestCrossTargetMapping:
    """Tests for cross-target path mapping in _filter_files_by_target."""

    def test_github_skills_mapped_to_claude(self):
        """Skills under .github/ are remapped to .claude/ when target=claude."""
        files = [
            ".github/skills/my-plugin/",
            ".github/skills/my-plugin/SKILL.md",
            ".github/skills/my-plugin/scripts/do-thing.sh",
        ]
        result, mappings = _filter_files_by_target(files, "claude")
        assert ".claude/skills/my-plugin/" in result
        assert ".claude/skills/my-plugin/SKILL.md" in result
        assert ".claude/skills/my-plugin/scripts/do-thing.sh" in result
        assert len(result) == 3
        assert len(mappings) == 3
        assert mappings[".claude/skills/my-plugin/SKILL.md"] == ".github/skills/my-plugin/SKILL.md"

    def test_claude_skills_mapped_to_copilot(self):
        """Reverse mapping: .claude/skills/ -> .github/skills/ for copilot."""
        files = [".claude/skills/review/SKILL.md"]
        result, mappings = _filter_files_by_target(files, "vscode")
        assert result == [".github/skills/review/SKILL.md"]
        assert mappings == {".github/skills/review/SKILL.md": ".claude/skills/review/SKILL.md"}

    def test_commands_not_mapped(self):
        """Commands are target-specific and must NOT be cross-mapped."""
        files = [".github/commands/run.md"]
        result, mappings = _filter_files_by_target(files, "claude")
        assert result == []
        assert mappings == {}

    def test_instructions_not_mapped(self):
        """Instructions are target-specific and must NOT be cross-mapped."""
        files = [".github/instructions/rules.md"]
        result, mappings = _filter_files_by_target(files, "claude")
        assert result == []
        assert mappings == {}

    def test_direct_match_not_double_mapped(self):
        """When file already matches target, it should not be remapped."""
        files = [
            ".claude/skills/review/SKILL.md",
            ".github/skills/review/SKILL.md",
        ]
        result, mappings = _filter_files_by_target(files, "claude")
        # Direct match exists, so no mapping needed
        assert ".claude/skills/review/SKILL.md" in result
        # The .github/ version should NOT create a duplicate
        assert result.count(".claude/skills/review/SKILL.md") == 1
        assert mappings == {}

    def test_mixed_direct_and_mapped(self):
        """Mix of direct matches and cross-mapped files."""
        files = [
            ".claude/commands/cmd.md",
            ".github/skills/my-skill/SKILL.md",
            ".github/agents/helper.md",
        ]
        result, mappings = _filter_files_by_target(files, "claude")
        assert ".claude/commands/cmd.md" in result
        assert ".claude/skills/my-skill/SKILL.md" in result
        assert ".claude/agents/helper.md" in result
        assert len(result) == 3
        assert len(mappings) == 2  # skills and agents mapped

    def test_cursor_mapping(self):
        """Skills under .github/ are remapped to .cursor/ when target=cursor."""
        files = [".github/skills/x/SKILL.md"]
        result, mappings = _filter_files_by_target(files, "cursor")
        assert result == [".cursor/skills/x/SKILL.md"]
        assert mappings == {".cursor/skills/x/SKILL.md": ".github/skills/x/SKILL.md"}

    def test_opencode_mapping(self):
        """Skills under .github/ are remapped to .opencode/ when target=opencode."""
        files = [".github/agents/a.md"]
        result, mappings = _filter_files_by_target(files, "opencode")
        assert result == [".opencode/agents/a.md"]
        assert mappings == {".opencode/agents/a.md": ".github/agents/a.md"}

    def test_all_target_no_mapping(self):
        """Target 'all' should include everything with no mapping."""
        files = [".github/skills/x/SKILL.md", ".claude/skills/y/SKILL.md"]
        result, mappings = _filter_files_by_target(files, "all")
        assert result == files
        assert mappings == {}

    def test_copilot_alias_same_as_vscode(self):
        """'copilot' target should produce same result as 'vscode' (deprecated alias)."""
        files = [".claude/skills/x/SKILL.md", ".claude/agents/a.md"]
        result_v, maps_v = _filter_files_by_target(files, "vscode")
        result_c, maps_c = _filter_files_by_target(files, "copilot")
        assert result_v == result_c
        assert maps_v == maps_c


class TestPackBundle:
    def test_pack_apm_format_copilot(self, tmp_path):
        deployed = [".github/agents/helper.agent.md", ".github/instructions/rules.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        out = tmp_path / "build"

        result = pack_bundle(project, out, fmt="apm")

        assert result.bundle_path == out / "test-pkg-1.0.0"
        assert set(result.files) == set(deployed)
        assert result.lockfile_enriched
        # Files exist in bundle
        for f in deployed:
            assert (result.bundle_path / f).exists()
        # Enriched lockfile present
        lock_content = (result.bundle_path / "apm.lock.yaml").read_text()
        assert "pack:" in lock_content

    def test_pack_apm_format_claude(self, tmp_path):
        deployed = [".claude/commands/cmd.md", ".claude/skills/s1/SKILL.md"]
        project = _setup_project(tmp_path, deployed, target="claude")
        out = tmp_path / "build"

        result = pack_bundle(project, out, fmt="apm")

        assert set(result.files) == set(deployed)
        for f in deployed:
            assert (result.bundle_path / f).exists()

    def test_pack_apm_format_all(self, tmp_path):
        deployed = [".github/agents/a.md", ".claude/commands/b.md"]
        project = _setup_project(tmp_path, deployed, target="all")
        out = tmp_path / "build"

        result = pack_bundle(project, out, fmt="apm")

        assert set(result.files) == set(deployed)

    def test_pack_archive(self, tmp_path):
        deployed = [".github/agents/a.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        out = tmp_path / "build"

        result = pack_bundle(project, out, archive=True)

        assert result.bundle_path.name == "test-pkg-1.0.0.tar.gz"
        assert result.bundle_path.exists()
        # The directory should be cleaned up
        assert not (out / "test-pkg-1.0.0").exists()
        # Archive is valid
        with tarfile.open(result.bundle_path, "r:gz") as tar:
            names = tar.getnames()
            assert any("a.md" in n for n in names)

    def test_pack_custom_output_dir(self, tmp_path):
        deployed = [".github/agents/a.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        custom_out = tmp_path / "custom" / "output"

        result = pack_bundle(project, custom_out)

        assert result.bundle_path.parent == custom_out
        assert result.bundle_path.exists()

    def test_pack_dry_run(self, tmp_path):
        deployed = [".github/agents/a.md", ".github/instructions/b.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        out = tmp_path / "build"

        result = pack_bundle(project, out, dry_run=True)

        assert set(result.files) == set(deployed)
        # Nothing written to disk
        assert not out.exists()

    def test_pack_no_lockfile_errors(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "apm.yml").write_text(
            yaml.dump({"name": "test", "version": "1.0.0"}), encoding="utf-8"
        )
        out = tmp_path / "build"

        with pytest.raises(FileNotFoundError, match="apm.lock.yaml not found"):
            pack_bundle(project, out)

    def test_pack_missing_deployed_file(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "apm.yml").write_text(
            yaml.dump({"name": "test", "version": "1.0.0"}), encoding="utf-8"
        )
        # Lock with a file that doesn't exist on disk
        lockfile = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            deployed_files=[".github/agents/ghost.md"],
        )
        lockfile.add_dependency(dep)
        lockfile.write(project / "apm.lock.yaml")
        out = tmp_path / "build"

        with pytest.raises(ValueError, match="missing on disk"):
            pack_bundle(project, out)

    def test_pack_empty_deployed_files(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "apm.yml").write_text(
            yaml.dump({"name": "test", "version": "1.0.0"}), encoding="utf-8"
        )
        lockfile = LockFile()
        dep = LockedDependency(repo_url="owner/repo", deployed_files=[])
        lockfile.add_dependency(dep)
        lockfile.write(project / "apm.lock.yaml")
        out = tmp_path / "build"

        result = pack_bundle(project, out)

        assert result.files == []
        assert result.bundle_path.exists()

    def test_pack_target_filtering(self, tmp_path):
        deployed = [".github/agents/a.md", ".claude/commands/b.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target="vscode")

        assert result.files == [".github/agents/a.md"]
        assert not (result.bundle_path / ".claude").exists()

    def test_pack_cross_target_mapping_github_to_claude(self, tmp_path):
        """Skills under .github/ are remapped into .claude/ in the bundle."""
        deployed = [
            ".github/skills/my-plugin/",
            ".github/skills/my-plugin/SKILL.md",
            ".github/skills/my-plugin/scripts/do-thing.sh",
        ]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target="claude")

        assert result.mapped_count == 3
        assert ".claude/skills/my-plugin/SKILL.md" in result.files
        # Bundle has files at the .claude/ path
        assert (result.bundle_path / ".claude/skills/my-plugin/SKILL.md").exists()
        assert (result.bundle_path / ".claude/skills/my-plugin/scripts/do-thing.sh").exists()
        # No .github/ files in bundle
        assert not (result.bundle_path / ".github").exists()

    def test_pack_cross_target_mapping_dry_run(self, tmp_path):
        """Dry-run with cross-target mapping returns correct files and mappings."""
        deployed = [".github/skills/x/SKILL.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target="claude", dry_run=True)

        assert ".claude/skills/x/SKILL.md" in result.files
        assert result.mapped_count == 1
        assert result.path_mappings[".claude/skills/x/SKILL.md"] == ".github/skills/x/SKILL.md"
        # Nothing written to disk
        assert not out.exists()

    def test_pack_cross_target_enriched_lockfile(self, tmp_path):
        """Enriched lockfile in bundle uses mapped paths and records mapped_from."""
        deployed = [".github/skills/x/SKILL.md", ".github/agents/a.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target="claude")

        lock_yaml = yaml.safe_load(
            (result.bundle_path / "apm.lock.yaml").read_text()
        )
        bundle_deployed = lock_yaml["dependencies"][0]["deployed_files"]
        assert ".claude/skills/x/SKILL.md" in bundle_deployed
        assert ".claude/agents/a.md" in bundle_deployed
        # mapped_from recorded in pack section
        assert "mapped_from" in lock_yaml["pack"]

    def test_pack_cross_target_no_double_map(self, tmp_path):
        """When both .github/ and .claude/ versions exist, no duplicate."""
        deployed = [
            ".github/skills/x/SKILL.md",
            ".claude/skills/x/SKILL.md",
        ]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target="claude")

        # Should contain .claude/ version (direct match), not duplicate
        assert result.files.count(".claude/skills/x/SKILL.md") == 1
        assert result.mapped_count == 0

    def test_pack_lockfile_enrichment(self, tmp_path):
        deployed = [".github/agents/a.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        out = tmp_path / "build"

        result = pack_bundle(project, out)

        lock_yaml = yaml.safe_load((result.bundle_path / "apm.lock.yaml").read_text())
        assert "pack" in lock_yaml
        assert lock_yaml["pack"]["format"] == "apm"
        assert lock_yaml["pack"]["target"] == "vscode"
        assert "packed_at" in lock_yaml["pack"]

    def test_pack_lockfile_original_unchanged(self, tmp_path):
        deployed = [".github/agents/a.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        out = tmp_path / "build"

        original_content = (project / "apm.lock.yaml").read_text()
        pack_bundle(project, out)

        assert (project / "apm.lock.yaml").read_text() == original_content

    def test_pack_rejects_embedded_traversal_in_deployed_path(self, tmp_path):
        """pack_bundle must reject path-traversal entries embedded in deployed_files."""
        project = _setup_project(tmp_path, [])
        # A path that looks like it starts with .github/ but traverses out
        lockfile = LockFile.read(project / "apm.lock.yaml")
        dep = LockedDependency(
            repo_url="owner/repo",
            deployed_files=[".github/../../../etc/passwd"],
        )
        lockfile.add_dependency(dep)
        lockfile.write(project / "apm.lock.yaml")

        with pytest.raises(ValueError, match="unsafe path"):
            pack_bundle(project, tmp_path / "out")


class TestPackSecurityScan:
    """Tests for hidden-Unicode scanning during pack (warn-only, never blocks)."""

    def test_pack_clean_files_no_warning(self, tmp_path):
        """Clean files produce no security warning."""
        deployed = [".github/agents/clean.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")
        out = tmp_path / "build"

        with patch("apm_cli.utils.console._rich_warning") as mock_warn:
            result = pack_bundle(project, out)

        mock_warn.assert_not_called()
        assert result.bundle_path.exists()
        assert set(result.files) == set(deployed)

    def test_pack_hidden_chars_warns_but_succeeds(self, tmp_path):
        """Files with hidden Unicode chars trigger a warning but bundle still succeeds."""
        deployed = [".github/agents/sneaky.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")

        # Inject a Unicode tag character (U+E0001) into the file
        sneaky = project / ".github/agents/sneaky.md"
        sneaky.write_text(f"Hello \U000E0001 world", encoding="utf-8")

        out = tmp_path / "build"

        with patch("apm_cli.utils.console._rich_warning") as mock_warn:
            result = pack_bundle(project, out)

        # Bundle created successfully — pack never blocks
        assert result.bundle_path.exists()
        assert (result.bundle_path / ".github/agents/sneaky.md").exists()
        # Warning was emitted about hidden characters
        mock_warn.assert_called_once()
        assert "hidden character" in mock_warn.call_args[0][0]

    def test_pack_skips_symlinks(self, tmp_path):
        """Symlinks are skipped during scanning — no crash, no findings from target."""
        deployed = [".github/agents/real.md", ".github/agents/link.md"]
        project = _setup_project(tmp_path, deployed, target="vscode")

        # Create a file with hidden chars inside the project tree
        poisoned = project / ".github/agents/poisoned.md"
        poisoned.write_text(f"hidden \U000E0001 payload", encoding="utf-8")

        # Replace link.md with a symlink to the poisoned file (within project)
        link_file = project / ".github/agents/link.md"
        link_file.unlink()
        try:
            os.symlink(poisoned, link_file)
        except OSError:
            pytest.skip("symlinks not supported on this platform")

        out = tmp_path / "build"

        with patch("apm_cli.utils.console._rich_warning") as mock_warn:
            result = pack_bundle(project, out)

        # No warning — the symlink target's hidden chars are not scanned
        mock_warn.assert_not_called()
        assert result.bundle_path.exists()


class TestPackBundleTraversalDeployed:
    def test_pack_rejects_traversal_deployed_path(self, tmp_path):
        """pack_bundle must reject path-traversal entries in deployed_files."""
        project = _setup_project(tmp_path, [])
        lockfile = LockFile.read(project / "apm.lock.yaml")
        dep = LockedDependency(
            repo_url="owner/repo",
            deployed_files=[".github/agents/../../../../../../tmp/evil.sh"],
        )
        lockfile.add_dependency(dep)
        lockfile.write(project / "apm.lock.yaml")

        with pytest.raises(ValueError, match="unsafe path"):
            pack_bundle(project, tmp_path / "out")


class TestFilterFilesByTargetList:
    """Tests for _filter_files_by_target with list target input."""

    def test_list_includes_union_of_prefixes(self):
        files = [".github/agents/a.md", ".claude/commands/b.md", ".cursor/rules/r.md"]
        result, mappings = _filter_files_by_target(files, ["claude", "vscode"])
        assert ".github/agents/a.md" in result
        assert ".claude/commands/b.md" in result
        assert ".cursor/rules/r.md" not in result
        assert mappings == {}

    def test_list_copilot_vscode_dedup(self):
        """copilot and vscode share .github/ prefix -- should not duplicate."""
        files = [".github/agents/a.md"]
        result, mappings = _filter_files_by_target(files, ["copilot", "vscode"])
        assert result == [".github/agents/a.md"]

    def test_list_single_element_matches_string(self):
        files = [".github/agents/a.md", ".claude/commands/b.md"]
        result_list, maps_list = _filter_files_by_target(files, ["vscode"])
        result_str, maps_str = _filter_files_by_target(files, "vscode")
        assert result_list == result_str
        assert maps_list == maps_str


class TestPackBundleMultiTarget:
    """Tests for pack_bundle with list targets."""

    def test_pack_list_target_dry_run(self, tmp_path):
        """List target passes through to filtering in dry-run mode."""
        deployed = [".github/agents/a.md", ".claude/commands/b.md", ".cursor/rules/r.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target=["claude", "vscode"], dry_run=True)

        assert ".github/agents/a.md" in result.files
        assert ".claude/commands/b.md" in result.files
        assert ".cursor/rules/r.md" not in result.files

    def test_pack_list_target_creates_bundle(self, tmp_path):
        """List target produces a valid bundle with files from all listed targets."""
        deployed = [".github/agents/a.md", ".claude/commands/b.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target=["claude", "vscode"])

        assert result.bundle_path.exists()
        assert (result.bundle_path / ".github/agents/a.md").exists()
        assert (result.bundle_path / ".claude/commands/b.md").exists()

    def test_pack_list_target_enriched_lockfile_target_string(self, tmp_path):
        """Enriched lockfile should have comma-joined target string."""
        deployed = [".github/agents/a.md", ".claude/commands/b.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        result = pack_bundle(project, out, target=["claude", "vscode"])

        lock_yaml = yaml.safe_load(
            (result.bundle_path / "apm.lock.yaml").read_text()
        )
        assert lock_yaml["pack"]["target"] == "claude,vscode"

    def test_pack_list_config_target_when_no_explicit(self, tmp_path):
        """When apm.yml has target: [claude, copilot] and no explicit --target."""
        deployed = [".github/agents/a.md", ".claude/commands/b.md"]
        project = _setup_project(tmp_path, deployed)
        out = tmp_path / "build"

        # Rewrite apm.yml with list target
        apm_yml = {"name": "test-pkg", "version": "1.0.0", "target": ["claude", "copilot"]}
        (project / "apm.yml").write_text(yaml.dump(apm_yml), encoding="utf-8")

        result = pack_bundle(project, out, target=None, dry_run=True)

        # Should include files from both .github/ (copilot) and .claude/ (claude)
        assert ".github/agents/a.md" in result.files
        assert ".claude/commands/b.md" in result.files
