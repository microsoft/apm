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

pytestmark = pytest.mark.component


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
        """``target: [claude, copilot]`` uses the shared parser while preserving
        Copilot's configuration destination. Multi-target lists stay as lists."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "target": ["claude", "copilot"],
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        assert pkg.target == ["claude", "copilot"]
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


class TestInvalidDependencyTypes:
    """Tests for non-dict dependency format rejection.

    Only the structured mapping (``dependencies: {apm: [...]}``) is
    supported. Any other type -- list, string, int -- must be rejected
    with a clear error.
    """

    def test_flat_list_dependencies_rejected(self, tmp_path):
        """Flat list dependencies raise ValueError."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": [
                    "owner/repo1#abc123",
                    "owner/repo2#def456",
                ],
            },
        )

        with pytest.raises(ValueError, match=r"expected a mapping.*got list"):
            APMPackage.from_apm_yml(yml)

    def test_flat_list_empty_dependencies_rejected(self, tmp_path):
        """Even an empty flat list is rejected."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": [],
            },
        )

        with pytest.raises(ValueError, match=r"expected a mapping.*got list"):
            APMPackage.from_apm_yml(yml)

    def test_flat_list_dev_dependencies_rejected(self, tmp_path):
        """Flat list devDependencies raise ValueError."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "devDependencies": [
                    "owner/dev-tool#v1",
                ],
            },
        )

        with pytest.raises(ValueError, match=r"expected a mapping.*got list"):
            APMPackage.from_apm_yml(yml)

    def test_string_dependencies_rejected(self, tmp_path):
        """String dependencies raise ValueError."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": "owner/repo",
            },
        )

        with pytest.raises(ValueError, match=r"expected a mapping.*got str"):
            APMPackage.from_apm_yml(yml)

    def test_int_dependencies_rejected(self, tmp_path):
        """Integer dependencies raise ValueError."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": 42,
            },
        )

        with pytest.raises(ValueError, match=r"expected a mapping.*got int"):
            APMPackage.from_apm_yml(yml)

    def test_error_mentions_structured_format(self, tmp_path):
        """Error message includes the correct structured format example."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": ["owner/repo"],
            },
        )

        with pytest.raises(ValueError, match=r"apm:"):
            APMPackage.from_apm_yml(yml)

    def test_structured_format_still_works(self, tmp_path):
        """Regression guard: structured format must not break."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test-pkg",
                "version": "1.0.0",
                "dependencies": {
                    "apm": ["owner/repo1"],
                    "mcp": [],
                },
            },
        )

        pkg = APMPackage.from_apm_yml(yml)

        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].repo_url == "owner/repo1"


class TestManifestDefaultHost:
    """OpenAPM req-mf-019: shorthand deps resolve against ``default_host:`` (#3843)."""

    def _deps(self, tmp_path: Path, **extra) -> APMPackage:
        data = {
            "name": "test-pkg",
            "version": "1.0.0",
            "dependencies": {"apm": ["acme/x", "github.com/acme/y"]},
            "devDependencies": {"apm": ["acme/dev-tool"]},
            **extra,
        }
        return APMPackage.from_apm_yml(_write_apm_yml(tmp_path, data))

    def test_shorthand_resolves_against_manifest_default_host(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        pkg = self._deps(tmp_path, default_host="gitlab.com")

        shorthand, fqdn = pkg.get_apm_dependencies()
        assert pkg.default_host == "gitlab.com"
        assert (shorthand.host, shorthand.repo_url) == ("gitlab.com", "acme/x")
        assert shorthand.get_identity() == "gitlab.com/acme/x"
        assert fqdn.host == "github.com"
        assert pkg.get_dev_apm_dependencies()[0].host == "gitlab.com"

    def test_omitted_default_host_keeps_env_contract(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        pkg = self._deps(tmp_path)

        assert pkg.default_host is None
        assert pkg.get_apm_dependencies()[0].host == "ghe.example.com"

    @pytest.mark.parametrize(
        "value", ["https://gitlab.com", "gitlab.com/acme", "gitlab.com:8443", "", 42]
    )
    def test_rejects_non_hostname_value(self, tmp_path, value):
        with pytest.raises(ValueError, match="default_host"):
            self._deps(tmp_path, default_host=value)

    def test_nested_manifest_does_not_inherit_parent_default_host(self, tmp_path):
        from apm_cli.utils.github_host import manifest_default_host_scope

        with manifest_default_host_scope("gitlab.com"):
            pkg = self._deps(tmp_path)

        assert pkg.get_apm_dependencies()[0].host == "github.com"

    def test_manifest_entry_strips_only_the_manifest_default_host(self, monkeypatch):
        from apm_cli.utils.github_host import manifest_default_host_scope

        monkeypatch.delenv("GITHUB_HOST", raising=False)
        with manifest_default_host_scope("gitlab.com"):
            gitlab = DependencyReference.parse("https://gitlab.com/acme/x.git")
            github = DependencyReference.parse("https://github.com/acme/y")
            assert gitlab.to_apm_yml_entry() == "acme/x"
            assert github.to_apm_yml_entry() == "github.com/acme/y"
            # Canonical identity strings stay manifest-independent.
            assert gitlab.to_canonical() == "gitlab.com/acme/x"
            assert github.to_canonical() == "acme/y"

    @pytest.mark.parametrize(
        ("manifest_host", "qualified", "entry", "identity"),
        [
            (
                "gitlab.com",
                "gitlab.com/group/sub/repo#v1",
                "group/sub/repo#v1",
                ("group/sub/repo", None),
            ),
            ("gitlab.com", "gitlab.com/acme/x", "acme/x", ("acme/x", None)),
            (
                "gitlab.com",
                "gitlab.com/acme/x/prompts/a.prompt.md",
                "acme/x/prompts/a.prompt.md",
                ("acme/x", "prompts/a.prompt.md"),
            ),
            (
                "gitlab.com",
                "gitlab.com/my.group/repo",
                "gitlab.com/my.group/repo",
                ("my.group/repo", None),
            ),
            (
                "dev.azure.com",
                "dev.azure.com/org/project/repo",
                "org/project/repo",
                ("org/project/repo", None),
            ),
            (None, "github.com/acme/y/skills/foo", "acme/y/skills/foo", ("acme/y", "skills/foo")),
        ],
    )
    def test_manifest_entry_round_trips_repository_boundary(
        self, monkeypatch, manifest_host, qualified, entry, identity
    ):
        from apm_cli.utils.github_host import manifest_default_host_scope

        monkeypatch.delenv("GITHUB_HOST", raising=False)
        expected_host = manifest_host or "github.com"
        with manifest_default_host_scope(manifest_host):
            dep = DependencyReference.parse(qualified)
            assert dep.to_apm_yml_entry() == entry
            reparsed = DependencyReference.parse(entry)
        for ref in (dep, reparsed):
            assert (ref.host, ref.repo_url, ref.virtual_path) == (expected_host, *identity)
        assert reparsed.get_identity() == dep.get_identity()
