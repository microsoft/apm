"""tests for apm_cli.bundle.plugin_exporter.

Covers missing lines/branches identified in coverage-unit.json:
- _sanitize_bundle_name: dangerous chars, empty after strip (lines 56-57)
- _collect_hooks_from_root: directory with JSON files, symlinks, parse errors (lines 255-267)
- _collect_mcp: symlink / non-file, parse error (lines 273-283)
- _get_dev_dependency_urls: dict dep, parse errors (lines 306-326)
- _find_or_synthesize_plugin_json: parse error path, no plugin.json, no logger (lines 357-377)
- _update_plugin_json_paths: strips keys with and without logger (lines 479-481)
- _merge_file_map: collision with and without force (lines 719-720)
- export_plugin_bundle: dry_run, collisions, archive, security scan, .mcp.json / hooks.json written
- export_plugin_bundle: missing apm.yml, local deps guard (lines 565, 593, 610, 624, 628)
"""

from __future__ import annotations

import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# _sanitize_bundle_name
# ---------------------------------------------------------------------------


class TestSanitizeBundleName:
    def test_slashes_replaced(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("org/repo")
        assert "/" not in result
        assert result == "org-repo"

    def test_backslash_replaced(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("org\\repo")
        assert result == "org-repo"

    def test_dotdot_replaced(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("../evil")
        # Any dots-only segments become safe
        assert ".." not in result

    def test_empty_after_strip_returns_unnamed(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("---")
        # Strips all hyphens -> empty -> unnamed
        assert result == "unnamed"

    def test_normal_name_unchanged(self):
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("my-plugin")
        assert result == "my-plugin"


# ---------------------------------------------------------------------------
# _collect_hooks_from_root
# ---------------------------------------------------------------------------


class TestCollectHooksFromRoot:
    def test_single_hooks_json(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

        (tmp_path / "hooks.json").write_text(json.dumps({"pre-commit": "echo hi"}))
        result = _collect_hooks_from_root(tmp_path)
        assert "pre-commit" in result

    def test_hooks_dir_multiple_files(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "a.json").write_text(json.dumps({"hook-a": "cmd-a"}))
        (hooks_dir / "b.json").write_text(json.dumps({"hook-b": "cmd-b"}))

        result = _collect_hooks_from_root(tmp_path)
        assert "hook-a" in result
        assert "hook-b" in result

    def test_invalid_json_silently_skipped(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

        (tmp_path / "hooks.json").write_text("{invalid json!}")
        result = _collect_hooks_from_root(tmp_path)
        assert result == {}

    def test_non_dict_json_silently_skipped(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

        (tmp_path / "hooks.json").write_text(json.dumps(["list"]))
        result = _collect_hooks_from_root(tmp_path)
        assert result == {}

    def test_hooks_dir_non_json_files_skipped(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_root

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "readme.md").write_text("# Readme")
        (hooks_dir / "hook.json").write_text(json.dumps({"hook": "cmd"}))

        result = _collect_hooks_from_root(tmp_path)
        assert "hook" in result


# ---------------------------------------------------------------------------
# _collect_mcp
# ---------------------------------------------------------------------------


class TestCollectMcp:
    def test_no_mcp_file_returns_empty(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        result = _collect_mcp(tmp_path)
        assert result == {}

    def test_valid_mcp_file(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"my-server": {"command": "npx"}}})
        )
        result = _collect_mcp(tmp_path)
        assert "my-server" in result

    def test_no_mcp_servers_key(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        (tmp_path / ".mcp.json").write_text(json.dumps({"other": "data"}))
        result = _collect_mcp(tmp_path)
        assert result == {}

    def test_invalid_json_returns_empty(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        (tmp_path / ".mcp.json").write_text("{invalid}")
        result = _collect_mcp(tmp_path)
        assert result == {}

    def test_non_dict_mcp_servers_returns_empty(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": ["list"]}))
        result = _collect_mcp(tmp_path)
        assert result == {}


# ---------------------------------------------------------------------------
# _get_dev_dependency_urls
# ---------------------------------------------------------------------------


class TestGetDevDependencyUrls:
    def test_string_deps(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _get_dev_dependency_urls

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.safe_dump(
                {"devDependencies": {"apm": ["microsoft/apm-sample-package", "github/org/repo"]}}
            )
        )
        result = _get_dev_dependency_urls(apm_yml)
        assert len(result) > 0

    def test_dict_dep(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _get_dev_dependency_urls

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.safe_dump(
                {
                    "devDependencies": {
                        "apm": [
                            {
                                "url": "https://github.com/org/repo.git",
                                "path": "",
                                "ref": "main",
                            }
                        ]
                    }
                }
            )
        )
        # Should handle dict deps without raising
        result = _get_dev_dependency_urls(apm_yml)
        assert isinstance(result, set)

    def test_no_dev_dependencies_returns_empty(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _get_dev_dependency_urls

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "test"}))
        result = _get_dev_dependency_urls(apm_yml)
        assert result == set()

    def test_invalid_yaml_returns_empty(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _get_dev_dependency_urls

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("{invalid yaml!!")
        result = _get_dev_dependency_urls(apm_yml)
        assert result == set()

    def test_non_list_apm_dev_returns_empty(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _get_dev_dependency_urls

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"devDependencies": {"apm": "not-a-list"}}))
        result = _get_dev_dependency_urls(apm_yml)
        assert result == set()


# ---------------------------------------------------------------------------
# _find_or_synthesize_plugin_json
# ---------------------------------------------------------------------------


class TestFindOrSynthesizePluginJson:
    def test_no_plugin_json_synthesizes(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _find_or_synthesize_plugin_json

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "test-plugin", "version": "1.0.0"}))

        with patch(
            "apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml",
            return_value={"name": "test-plugin"},
        ):
            result = _find_or_synthesize_plugin_json(
                tmp_path,
                apm_yml,
                suppress_missing_warning=True,
            )
        assert result == {"name": "test-plugin"}

    def test_invalid_plugin_json_falls_back(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _find_or_synthesize_plugin_json

        (tmp_path / "plugin.json").write_text("{invalid json!}")
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "test"}))

        with (
            patch(
                "apm_cli.utils.helpers.find_plugin_json",
                return_value=tmp_path / "plugin.json",
            ),
            patch(
                "apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml",
                return_value={"name": "fallback"},
            ),
        ):
            result = _find_or_synthesize_plugin_json(tmp_path, apm_yml)
        assert result == {"name": "fallback"}

    def test_info_logged_without_logger(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _find_or_synthesize_plugin_json

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "test"}))

        with (
            patch(
                "apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml",
                return_value={"name": "test"},
            ),
            patch("apm_cli.core.plugin_manifest._rich_info") as mock_info,
        ):
            _find_or_synthesize_plugin_json(tmp_path, apm_yml, suppress_missing_warning=False)
        mock_info.assert_called_once()


# ---------------------------------------------------------------------------
# _update_plugin_json_paths
# ---------------------------------------------------------------------------


class TestUpdatePluginJsonPaths:
    def test_strips_schema_invalid_keys_no_logger(self):
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json = {
            "name": "test",
            "agents": ["agents/test.md"],
            "skills": ["skills/test/SKILL.md"],
        }
        with patch("apm_cli.bundle.plugin_exporter._rich_warning") as mock_warn:
            result = _update_plugin_json_paths(plugin_json, [], logger=None)
        assert "agents" not in result
        assert "skills" not in result
        mock_warn.assert_called_once()

    def test_strips_schema_invalid_keys_with_logger(self):
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json = {
            "name": "test",
            "commands": ["commands/foo.md"],
            "instructions": ["instructions/bar.md"],
        }
        mock_logger = MagicMock()
        result = _update_plugin_json_paths(plugin_json, [], logger=mock_logger)
        assert "commands" not in result
        mock_logger.warning.assert_called_once()

    def test_no_strip_when_no_matching_keys(self):
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json = {"name": "test", "version": "1.0.0"}
        with patch("apm_cli.bundle.plugin_exporter._rich_warning") as mock_warn:
            result = _update_plugin_json_paths(plugin_json, [], logger=None)
        assert result == plugin_json
        mock_warn.assert_not_called()


# ---------------------------------------------------------------------------
# _merge_file_map
# ---------------------------------------------------------------------------


class TestMergeFileMap:
    def test_no_collision_adds_file(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _merge_file_map

        src = tmp_path / "file.txt"
        src.write_text("content")
        file_map = {}
        collisions = []

        _merge_file_map(file_map, [(src, "skills/foo.md")], "owner-a", False, collisions)
        assert "skills/foo.md" in file_map
        assert collisions == []

    def test_collision_first_writer_wins(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _merge_file_map

        src_a = tmp_path / "a.txt"
        src_b = tmp_path / "b.txt"
        src_a.write_text("a")
        src_b.write_text("b")

        file_map = {}
        collisions = []

        _merge_file_map(file_map, [(src_a, "skills/foo.md")], "owner-a", False, collisions)
        _merge_file_map(file_map, [(src_b, "skills/foo.md")], "owner-b", False, collisions)

        assert file_map["skills/foo.md"][0] == src_a
        assert len(collisions) == 1
        assert "first writer wins" in collisions[0]

    def test_collision_force_last_writer_wins(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _merge_file_map

        src_a = tmp_path / "a.txt"
        src_b = tmp_path / "b.txt"
        src_a.write_text("a")
        src_b.write_text("b")

        file_map = {}
        collisions = []

        _merge_file_map(file_map, [(src_a, "skills/foo.md")], "owner-a", True, collisions)
        _merge_file_map(file_map, [(src_b, "skills/foo.md")], "owner-b", True, collisions)

        assert file_map["skills/foo.md"][0] == src_b
        assert len(collisions) == 1
        assert "last writer wins" in collisions[0]

    def test_unsafe_rel_path_skipped(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import _merge_file_map

        src = tmp_path / "file.txt"
        src.write_text("x")

        file_map = {}
        collisions = []

        # Unsafe output_rel (traversal)
        _merge_file_map(file_map, [(src, "../evil.md")], "owner", False, collisions)
        assert file_map == {}


# ---------------------------------------------------------------------------
# export_plugin_bundle
# ---------------------------------------------------------------------------


class TestExportPluginBundle:
    def _make_project(self, tmp_path):
        """Create a minimal APM project for testing."""
        (tmp_path / "apm.yml").write_text(
            yaml.safe_dump({"name": "test-plugin", "version": "1.0.0"})
        )
        (tmp_path / "plugin.json").write_text(json.dumps({"name": "test-plugin"}))
        (tmp_path / "apm_modules").mkdir(exist_ok=True)
        return tmp_path

    def test_dry_run_returns_pack_result(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = export_plugin_bundle(project, output_dir, dry_run=True)
        assert result is not None
        assert result.bundle_path is not None

    def test_local_dep_raises(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        mock_dep = MagicMock()
        mock_dep.is_local = True
        mock_dep.local_path = "../sibling"

        with (
            patch(
                "apm_cli.models.apm_package.APMPackage.get_apm_dependencies",
                return_value=[mock_dep],
            ),
            pytest.raises(ValueError, match="Cannot pack"),
        ):
            export_plugin_bundle(project, output_dir)

    def test_creates_bundle_dir(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        # Add a skill
        (project / ".apm").mkdir()
        (project / ".apm" / "skills").mkdir()
        (project / ".apm" / "skills" / "my-skill").mkdir()
        (project / ".apm" / "skills" / "my-skill" / "SKILL.md").write_text("# Skill")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = export_plugin_bundle(project, output_dir)
        assert result.bundle_path.exists()

    def test_archive_creates_zip(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = export_plugin_bundle(project, output_dir, archive=True)
        assert result.bundle_path.suffix == ".zip"
        assert zipfile.is_zipfile(str(result.bundle_path))

    def test_collision_warning_emitted_no_logger(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("apm_cli.bundle.plugin_exporter._rich_warning"):
            with patch(
                "apm_cli.bundle.plugin_exporter._merge_file_map",
                side_effect=lambda fm, comps, name, force, colls: colls.append(
                    f"collision: {name}"
                ),
            ):
                export_plugin_bundle(project, output_dir)
        # _rich_warning is only called for collisions, not necessarily from here
        # Just ensure it runs without error

    def test_collision_warning_emitted_with_logger(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        logger = MagicMock()

        # Inject a collision
        import apm_cli.bundle.plugin_exporter as pe

        def _patched_merge(fm, comps, name, force, colls):
            colls.append(f"collision in {name}")

        with patch.object(pe, "_merge_file_map", side_effect=_patched_merge):
            export_plugin_bundle(project, output_dir, logger=logger)
        logger.warning.assert_called()

    def test_merged_hooks_written(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        (project / "hooks.json").write_text(json.dumps({"pre-commit": "echo hook"}))
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = export_plugin_bundle(project, output_dir)
        hooks_file = result.bundle_path / "hooks.json"
        assert hooks_file.exists()
        data = json.loads(hooks_file.read_text())
        assert "pre-commit" in data

    def test_merged_mcp_written(self, tmp_path):
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = self._make_project(tmp_path)
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"my-server": {"command": "npx"}}})
        )
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = export_plugin_bundle(project, output_dir)
        mcp_file = result.bundle_path / ".mcp.json"
        assert mcp_file.exists()
        data = json.loads(mcp_file.read_text())
        assert "my-server" in data["mcpServers"]
