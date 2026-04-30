"""Unit tests for APMPackage devDependencies support."""

from pathlib import Path

import pytest
import yaml

from apm_cli.models.apm_package import (
    APMPackage,
    DependencyReference,
    MCPDependency,
    clear_apm_yml_cache,
)


def _write_apm_yml(tmp_path: Path, data: dict) -> Path:
    """Write an apm.yml file and return its path."""
    clear_apm_yml_cache()
    path = tmp_path / "apm.yml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


class TestDevDependencies:
    """Tests for devDependencies support in APMPackage."""

    def test_parse_dev_dependencies(self, tmp_path):
        """devDependencies section is parsed from apm.yml."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "apm": ["owner/dev-tool"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.dev_dependencies is not None
        assert "apm" in pkg.dev_dependencies

    def test_get_dev_apm_dependencies(self, tmp_path):
        """get_dev_apm_dependencies returns DependencyReference objects."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "apm": ["owner/dev-tool", "org/test-helper"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)
        dev_deps = pkg.get_dev_apm_dependencies()

        assert len(dev_deps) == 2
        assert all(isinstance(d, DependencyReference) for d in dev_deps)
        urls = {d.repo_url for d in dev_deps}
        assert "owner/dev-tool" in urls
        assert "org/test-helper" in urls

    def test_get_dev_mcp_dependencies(self, tmp_path):
        """get_dev_mcp_dependencies returns MCPDependency objects."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "mcp": [
                        {"name": "io.github.test/mcp-server", "transport": "stdio"},
                    ],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)
        dev_mcp = pkg.get_dev_mcp_dependencies()

        assert len(dev_mcp) == 1
        assert isinstance(dev_mcp[0], MCPDependency)
        assert dev_mcp[0].name == "io.github.test/mcp-server"
        assert dev_mcp[0].transport == "stdio"

    def test_get_dev_mcp_from_string(self, tmp_path):
        """MCP dev dependencies can be specified as plain strings."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "mcp": ["io.github.test/mcp-server"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)
        dev_mcp = pkg.get_dev_mcp_dependencies()

        assert len(dev_mcp) == 1
        assert dev_mcp[0].name == "io.github.test/mcp-server"

    def test_missing_dev_dependencies_returns_empty(self, tmp_path):
        """No devDependencies section returns empty lists."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.dev_dependencies is None
        assert pkg.get_dev_apm_dependencies() == []
        assert pkg.get_dev_mcp_dependencies() == []

    def test_empty_dev_dependencies_returns_empty(self, tmp_path):
        """Empty devDependencies.apm list returns empty."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {"apm": []},
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.dev_dependencies is not None
        assert pkg.get_dev_apm_dependencies() == []

    def test_dev_and_prod_dependencies_independent(self, tmp_path):
        """devDeps and deps are independent — changing one doesn't affect other."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": {
                    "apm": ["owner/prod-dep"],
                },
                "devDependencies": {
                    "apm": ["owner/dev-dep"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        prod_deps = pkg.get_apm_dependencies()
        dev_deps = pkg.get_dev_apm_dependencies()

        assert len(prod_deps) == 1
        assert len(dev_deps) == 1
        assert prod_deps[0].repo_url == "owner/prod-dep"
        assert dev_deps[0].repo_url == "owner/dev-dep"

    def test_dev_dependencies_do_not_pollute_prod(self, tmp_path):
        """Dev dependencies don't appear in get_apm_dependencies()."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": {"apm": []},
                "devDependencies": {
                    "apm": ["owner/dev-only"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        prod_urls = {d.repo_url for d in pkg.get_apm_dependencies()}
        assert "owner/dev-only" not in prod_urls

    def test_dev_dependencies_dict_format(self, tmp_path):
        """devDependencies support dict-format entries (Cargo-style git objects)."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "apm": [
                        {"git": "https://github.com/owner/complex-dep.git", "ref": "main"},
                    ],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)
        dev_deps = pkg.get_dev_apm_dependencies()

        assert len(dev_deps) == 1
        assert isinstance(dev_deps[0], DependencyReference)

    def test_mixed_dev_dependency_types(self, tmp_path):
        """devDependencies can have both apm and mcp types."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "apm": ["owner/dev-apm"],
                    "mcp": ["io.github.test/mcp-debug"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert len(pkg.get_dev_apm_dependencies()) == 1
        assert len(pkg.get_dev_mcp_dependencies()) == 1

    def test_dev_apm_no_mcp_key(self, tmp_path):
        """get_dev_mcp_dependencies returns empty when only apm devDeps exist."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": {
                    "apm": ["owner/dev-tool"],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.get_dev_mcp_dependencies() == []


class TestTargetField:
    """Tests for target field supporting both str and list[str]."""

    def test_target_string(self, tmp_path):
        """target: copilot → stored as string."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "target": "copilot",
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.target == "copilot"
        assert isinstance(pkg.target, str)

    def test_target_list(self, tmp_path):
        """``target: [claude, copilot]`` is now alias-resolved through the
        shared parser -- ``copilot`` collapses to its canonical name
        ``vscode`` (#820).  Multi-target lists stay as lists."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "target": ["claude", "copilot"],
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.target == ["claude", "vscode"]
        assert isinstance(pkg.target, list)

    def test_target_missing(self, tmp_path):
        """No target field → None."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.target is None

    def test_target_single_item_list(self, tmp_path):
        """A single-element list (``target: [copilot]``) collapses to a
        plain string -- the shared parser canonicalizes ``str`` and
        ``[str]`` to the same shape so downstream code only ever sees one
        ``Union[str, List[str]]`` form per cardinality (#820)."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "target": ["copilot"],
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.target == "copilot"
        assert isinstance(pkg.target, str)

    def test_target_direct_construction_string(self):
        """APMPackage can be constructed with target as string."""
        pkg = APMPackage(name="t", version="1.0.0", target="claude")
        assert pkg.target == "claude"

    def test_target_direct_construction_list(self):
        """APMPackage can be constructed with target as list."""
        pkg = APMPackage(name="t", version="1.0.0", target=["claude", "copilot"])
        assert pkg.target == ["claude", "copilot"]


class TestClearCache:
    """Tests for clear_apm_yml_cache."""

    def test_clear_forces_reparse(self, tmp_path):
        """After clear, the same file is re-parsed (not cached)."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
            },
        )

        pkg1 = APMPackage.from_apm_yml(yml)

        # Overwrite with different data
        clear_apm_yml_cache()
        yml.write_text(
            yaml.dump(
                {
                    "name": "changed-pkg",
                    "version": "2.0.0",
                }
            ),
            encoding="utf-8",
        )

        pkg2 = APMPackage.from_apm_yml(yml)

        assert pkg1.name == "test-pkg"
        assert pkg2.name == "changed-pkg"


class TestIncludesField:
    """Tests for the 'includes' field on APMPackage (auto-publish opt-in)."""

    def test_includes_auto_parses_to_string(self, tmp_path):
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "includes": "auto",
            },
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.includes == "auto"

    def test_includes_list_parses_to_list(self, tmp_path):
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "includes": ["path/a", "path/b"],
            },
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.includes == ["path/a", "path/b"]

    def test_includes_missing_is_none(self, tmp_path):
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
            },
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.includes is None

    def test_includes_invalid_int_raises(self, tmp_path):
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "includes": 42,
            },
        )
        with pytest.raises(ValueError, match="'includes' must be 'auto' or a list of strings"):
            APMPackage.from_apm_yml(yml)

    def test_includes_list_with_non_strings_raises(self, tmp_path):
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "includes": [1, 2],
            },
        )
        with pytest.raises(ValueError, match="'includes' must be 'auto' or a list of strings"):
            APMPackage.from_apm_yml(yml)

    def test_includes_other_string_raises(self, tmp_path):
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "includes": "explicit",
            },
        )
        with pytest.raises(ValueError, match="'includes' must be 'auto' or a list of strings"):
            APMPackage.from_apm_yml(yml)

    def test_has_apm_dependencies_false_for_include_only_manifest(self, tmp_path):
        """Include-only manifests (no apm: deps) still report no APM dependencies."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "includes": "auto",
            },
        )
        pkg = APMPackage.from_apm_yml(yml)
        assert pkg.has_apm_dependencies() is False
