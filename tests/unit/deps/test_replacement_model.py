"""Unit tests for the replacement model: parsing, validation, selector normalization,
and manifest contract gate.

Tests the ``parse_replace_block`` parser, ``ReplacementSelector`` normalization,
``ReplacementRule`` construction, error handling for malformed/ambiguous
declarations, and the D3 contract gate that requires ``source-replacements-v1``
``$schema`` when ``replace`` is present.
"""

import pytest

from apm_cli.models.dependency.replacement import (
    GitReplacementSource,
    LocalReplacementSource,
    ReplacementConfigError,
    ReplacementIndex,
    ReplacementRule,
    ReplacementSelector,
    build_replacement_index,
    parse_replace_block,
    replacement_rule_to_dict,
    replacement_rules_from_dicts,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Happy path: valid replace blocks
# ---------------------------------------------------------------------------


class TestParseReplaceBlockValid:
    """Valid ``replace:`` block parsing."""

    def test_empty_list(self):
        """Empty replace list produces no rules."""
        assert parse_replace_block([]) == []

    def test_none_produces_empty(self):
        """None (absent key) produces no rules."""
        assert parse_replace_block(None) == []

    def test_local_replacement_basic(self):
        """Local path replacement parses correctly."""
        raw = [
            {
                "for": {
                    "git": "https://github.com/example-org/catalog.git",
                    "path": "packages/base",
                },
                "with": {"path": "../catalog/packages/base"},
            }
        ]
        rules = parse_replace_block(raw)
        assert len(rules) == 1
        rule = rules[0]
        assert rule.selector.host == "github.com"
        assert rule.selector.port is None
        assert rule.selector.repo_url == "example-org/catalog"
        assert rule.selector.subpath == "packages/base"
        assert isinstance(rule.source, LocalReplacementSource)
        assert rule.source.path == "../catalog/packages/base"

    def test_git_replacement_basic(self):
        """Git fork replacement parses correctly."""
        raw = [
            {
                "for": {
                    "git": "https://github.com/example-org/catalog.git",
                    "path": "packages/base",
                },
                "with": {
                    "git": "https://github.com/my-org/catalog-fork.git",
                    "path": "packages/base",
                    "ref": "patched-base",
                },
            }
        ]
        rules = parse_replace_block(raw)
        assert len(rules) == 1
        rule = rules[0]
        assert rule.selector.host == "github.com"
        assert rule.selector.repo_url == "example-org/catalog"
        assert rule.selector.subpath == "packages/base"
        assert isinstance(rule.source, GitReplacementSource)
        assert rule.source.repo_url == "my-org/catalog-fork"
        assert rule.source.ref == "patched-base"
        assert rule.source.subpath == "packages/base"

    def test_repo_root_selector_no_path(self):
        """Selector without path targets the repository root."""
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo.git"},
                "with": {"path": "../local-repo"},
            }
        ]
        rules = parse_replace_block(raw)
        assert len(rules) == 1
        assert rules[0].selector.subpath is None

    def test_git_source_without_path(self):
        """Git source without path keeps subpath None."""
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo"},
                "with": {
                    "git": "https://github.com/fork-owner/repo.git",
                    "ref": "fix-branch",
                },
            }
        ]
        rules = parse_replace_block(raw)
        src = rules[0].source
        assert isinstance(src, GitReplacementSource)
        assert src.subpath is None

    def test_multiple_rules(self):
        """Multiple rules parse in declaration order."""
        raw = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local-b"},
            },
            {
                "for": {"git": "https://github.com/c/d.git", "path": "pkg"},
                "with": {
                    "git": "https://github.com/e/f.git",
                    "ref": "main",
                },
            },
        ]
        rules = parse_replace_block(raw)
        assert len(rules) == 2
        assert rules[0].selector.repo_url == "a/b"
        assert rules[1].selector.repo_url == "c/d"

    def test_non_github_host(self):
        """Non-GitHub supported host parses correctly."""
        raw = [
            {
                "for": {"git": "https://gitlab.com/team/project.git"},
                "with": {"path": "./local-project"},
            }
        ]
        rules = parse_replace_block(raw)
        assert rules[0].selector.host == "gitlab.com"

    def test_host_casing_normalized(self):
        """Host is lowercased."""
        raw = [
            {
                "for": {"git": "https://GitHub.COM/Owner/Repo.git"},
                "with": {"path": "./local"},
            }
        ]
        rules = parse_replace_block(raw)
        assert rules[0].selector.host == "github.com"
        # GitHub repos are case-insensitive
        assert rules[0].selector.repo_url == "owner/repo"

    def test_default_port_stripped(self):
        """Default HTTPS port (443) is normalized to None."""
        raw = [
            {
                "for": {"git": "https://github.com:443/owner/repo.git"},
                "with": {"path": "./local"},
            }
        ]
        rules = parse_replace_block(raw)
        assert rules[0].selector.port is None

    def test_custom_port_preserved(self):
        """Non-default port is preserved."""
        raw = [
            {
                "for": {"git": "https://ghe.corp.example.com:8443/owner/repo.git"},
                "with": {"path": "./local"},
            }
        ]
        rules = parse_replace_block(raw)
        assert rules[0].selector.port == 8443

    def test_subpath_normalized(self):
        """Subpath trailing/leading slashes and backslashes are normalized."""
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo.git", "path": " /packages/base/ "},
                "with": {"path": "./local"},
            }
        ]
        rules = parse_replace_block(raw)
        assert rules[0].selector.subpath == "packages/base"

    def test_local_sibling_checkout(self):
        """Sibling checkout (../) is accepted."""
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo.git"},
                "with": {"path": "../sibling-repo"},
            }
        ]
        rules = parse_replace_block(raw)
        src = rules[0].source
        assert isinstance(src, LocalReplacementSource)
        assert src.path == "../sibling-repo"

    def test_absolute_local_path(self):
        """Absolute local path is accepted."""
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo.git"},
                "with": {"path": "/opt/packages/repo"},
            }
        ]
        rules = parse_replace_block(raw)
        src = rules[0].source
        assert isinstance(src, LocalReplacementSource)
        assert src.path == "/opt/packages/repo"


# ---------------------------------------------------------------------------
# Error cases: structural and type violations
# ---------------------------------------------------------------------------


class TestParseReplaceBlockErrors:
    """Error handling in ``parse_replace_block``."""

    def test_not_a_list(self):
        with pytest.raises(ReplacementConfigError, match="must be a list"):
            parse_replace_block("not-a-list")

    def test_entry_not_a_dict(self):
        with pytest.raises(ReplacementConfigError, match="must be a mapping"):
            parse_replace_block(["not-a-dict"])

    def test_missing_for(self):
        with pytest.raises(ReplacementConfigError, match="missing required 'for'"):
            parse_replace_block([{"with": {"path": "./local"}}])

    def test_missing_with(self):
        with pytest.raises(ReplacementConfigError, match="missing required 'with'"):
            parse_replace_block([{"for": {"git": "https://github.com/a/b"}}])

    def test_unknown_top_level_field(self):
        with pytest.raises(ReplacementConfigError, match="unknown field"):
            parse_replace_block(
                [{"for": {"git": "https://github.com/a/b"}, "with": {"path": "./x"}, "extra": 1}]
            )

    def test_selector_missing_git(self):
        with pytest.raises(ReplacementConfigError, match="'git' field is required"):
            parse_replace_block([{"for": {"path": "pkg"}, "with": {"path": "./x"}}])

    def test_selector_unknown_field(self):
        with pytest.raises(ReplacementConfigError, match="unknown field"):
            parse_replace_block(
                [{"for": {"git": "https://github.com/a/b", "ref": "main"}, "with": {"path": "./x"}}]
            )

    def test_selector_shorthand_rejected(self):
        """Shorthand (non-URL) git values are rejected."""
        with pytest.raises(ReplacementConfigError, match="explicit supported Git URL"):
            parse_replace_block([{"for": {"git": "owner/repo"}, "with": {"path": "./x"}}])

    def test_selector_ssh_uses_existing_git_parser(self):
        """An explicit SSH locator identifies the same package as HTTPS."""
        rules = parse_replace_block(
            [{"for": {"git": "git@github.com:owner/repo.git"}, "with": {"path": "./x"}}]
        )
        assert rules[0].selector.key == ("github.com", None, "owner/repo", None)

    def test_source_missing_ref_for_git(self):
        """Git source without ref is rejected."""
        with pytest.raises(ReplacementConfigError, match="requires an explicit 'ref'"):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"git": "https://github.com/c/d"},
                    }
                ]
            )

    def test_source_empty_git(self):
        with pytest.raises(ReplacementConfigError, match="non-empty string"):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"git": "", "ref": "main"},
                    }
                ]
            )

    def test_source_unknown_field(self):
        with pytest.raises(ReplacementConfigError, match="unknown field"):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"path": "./x", "alias": "y"},
                    }
                ]
            )

    def test_source_no_path_no_git(self):
        with pytest.raises(ReplacementConfigError, match="must contain either"):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"ref": "main"},
                    }
                ]
            )

    def test_source_local_not_local_path(self):
        """Path that doesn't look like a local path is rejected."""
        with pytest.raises(ReplacementConfigError, match="must start with"):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"path": "not-a-local-path"},
                    }
                ]
            )

    def test_selector_empty_git(self):
        with pytest.raises(ReplacementConfigError, match="non-empty string"):
            parse_replace_block([{"for": {"git": ""}, "with": {"path": "./x"}}])

    def test_selector_git_not_string(self):
        with pytest.raises(ReplacementConfigError, match="non-empty string"):
            parse_replace_block([{"for": {"git": 42}, "with": {"path": "./x"}}])

    def test_selector_invalid_repo_path(self):
        """URL with no valid repo path is rejected."""
        with pytest.raises(ReplacementConfigError, match="Invalid repository URL path"):
            parse_replace_block([{"for": {"git": "https://github.com/"}, "with": {"path": "./x"}}])

    def test_source_empty_ref(self):
        with pytest.raises(ReplacementConfigError, match="non-empty string"):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"git": "https://github.com/c/d", "ref": ""},
                    }
                ]
            )

    def test_for_block_not_dict(self):
        with pytest.raises(ReplacementConfigError, match="expected a mapping"):
            parse_replace_block([{"for": "https://github.com/a/b", "with": {"path": "./x"}}])

    def test_with_block_not_dict(self):
        with pytest.raises(ReplacementConfigError, match="expected a mapping"):
            parse_replace_block([{"for": {"git": "https://github.com/a/b"}, "with": "./x"}])

    def test_subpath_traversal_rejected(self):
        """Path traversal in subpath is rejected."""
        with pytest.raises((ReplacementConfigError, ValueError)):
            parse_replace_block(
                [
                    {
                        "for": {"git": "https://github.com/a/b", "path": "../escape"},
                        "with": {"path": "./x"},
                    }
                ]
            )


# ---------------------------------------------------------------------------
# Selector identity and normalization
# ---------------------------------------------------------------------------


class TestReplacementSelector:
    """Selector coordinate normalization and identity."""

    def test_key_tuple(self):
        sel = ReplacementSelector(host="github.com", port=None, repo_url="owner/repo", subpath=None)
        assert sel.key == ("github.com", None, "owner/repo", None)

    def test_key_with_subpath(self):
        sel = ReplacementSelector(
            host="github.com", port=None, repo_url="owner/repo", subpath="packages/base"
        )
        assert sel.key == ("github.com", None, "owner/repo", "packages/base")

    def test_key_with_port(self):
        sel = ReplacementSelector(
            host="ghe.corp.com", port=8443, repo_url="owner/repo", subpath=None
        )
        assert sel.key == ("ghe.corp.com", 8443, "owner/repo", None)

    def test_root_and_subpath_are_distinct(self):
        """Repository root and its subdirectory are distinct selectors."""
        root = ReplacementSelector(host="github.com", port=None, repo_url="a/b", subpath=None)
        sub = ReplacementSelector(host="github.com", port=None, repo_url="a/b", subpath="pkg")
        assert root.key != sub.key

    def test_different_hosts_are_distinct(self):
        """Same repo path on different hosts are distinct selectors."""
        gh = ReplacementSelector(host="github.com", port=None, repo_url="a/b", subpath=None)
        gl = ReplacementSelector(host="gitlab.com", port=None, repo_url="a/b", subpath=None)
        assert gh.key != gl.key


# ---------------------------------------------------------------------------
# APMPackage.from_mapping integration
# ---------------------------------------------------------------------------

_SOURCE_REPLACEMENTS_V1_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


class TestAPMPackageReplaceIntegration:
    """``replace:`` parsed through ``APMPackage.from_mapping``."""

    def _base_manifest(self, **extra):
        data = {"name": "test-pkg", "version": "1.0.0", **extra}
        return data

    def _replace_manifest(self, **extra):
        """Base manifest with source-replacements-v1 contract."""
        return self._base_manifest(**{"$schema": _SOURCE_REPLACEMENTS_V1_SCHEMA, **extra})

    def test_no_replace_key(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage.from_mapping(
            self._base_manifest(),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is None

    def test_empty_replace_list(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage.from_mapping(
            self._replace_manifest(replace=[]),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules == []

    def test_valid_replace_parsed(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        manifest = self._replace_manifest(
            replace=[
                {
                    "for": {"git": "https://github.com/org/lib.git"},
                    "with": {"path": "../local-lib"},
                }
            ]
        )
        pkg = APMPackage.from_mapping(
            manifest,
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is not None
        assert len(pkg.replace_rules) == 1
        assert pkg.replace_rules[0].selector.repo_url == "org/lib"

    def test_malformed_replace_raises_valueerror(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        manifest = self._replace_manifest(replace="not-a-list")
        with pytest.raises(ValueError, match="must be a list"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_not_in_unknown_keys(self, tmp_path):
        """``replace`` does not trigger an unknown-key warning."""
        from apm_cli.models.apm_package import APMPackage

        warnings: list = []

        def capture_warning(keys, path):
            warnings.extend(keys)

        APMPackage.from_mapping(
            self._replace_manifest(
                replace=[
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"path": "./local"},
                    }
                ]
            ),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=capture_warning,
        )
        assert "replace" not in warnings

    # -- Contract gate tests (D3) ------------------------------------------

    def test_replace_without_schema_rejects(self, tmp_path):
        """``replace`` key without source-replacements-v1 ``$schema`` is rejected."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._base_manifest(replace=[])
        with pytest.raises(ValueError, match="source-replacements-v1"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_with_working_draft_rejects(self, tmp_path):
        """Working-draft (no ``$schema``) with ``replace`` is rejected."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._base_manifest(replace=[])
        with pytest.raises(ValueError, match="source-replacements-v1"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_with_openapm_v01_rejects(self, tmp_path):
        """OpenAPM v0.1 ``$schema`` with ``replace`` is rejected."""
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.manifest_contract import OPENAPM_V01_SCHEMA_URI

        manifest = self._base_manifest(
            **{"$schema": OPENAPM_V01_SCHEMA_URI},
            replace=[],
        )
        with pytest.raises(ValueError, match="source-replacements-v1"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_with_correct_schema_accepts(self, tmp_path):
        """Source-replacements-v1 ``$schema`` with ``replace`` is accepted."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._replace_manifest(replace=[])
        pkg = APMPackage.from_mapping(
            manifest,
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules == []
        assert pkg.manifest_contract == "source-replacements-v1"

    def test_no_replace_with_correct_schema_accepted(self, tmp_path):
        """Source-replacements-v1 ``$schema`` is valid even without ``replace``."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._base_manifest(**{"$schema": _SOURCE_REPLACEMENTS_V1_SCHEMA})
        pkg = APMPackage.from_mapping(
            manifest,
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is None
        assert pkg.manifest_contract == "source-replacements-v1"

    def test_no_replace_without_schema_is_working_draft(self, tmp_path):
        """Absent ``replace`` and absent ``$schema`` remains working-draft."""
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage.from_mapping(
            self._base_manifest(),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is None
        assert pkg.manifest_contract == "working-draft"


# ---------------------------------------------------------------------------
# ReplacementIndex
# ---------------------------------------------------------------------------


class TestReplacementIndex:
    """Index construction and lookup."""

    def _make_rule(self, host="github.com", repo="a/b", subpath=None, local_path="./x"):
        sel = ReplacementSelector(host=host, port=None, repo_url=repo, subpath=subpath)
        src = LocalReplacementSource(path=local_path)
        return ReplacementRule(selector=sel, source=src)

    def test_empty_index(self):
        idx = ReplacementIndex.empty()
        assert len(idx) == 0
        assert not idx

    def test_single_rule_lookup(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="owner/repo", host="github.com")
        assert idx.lookup(dep) is rule

    def test_no_match(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="other/repo", host="github.com")
        assert idx.lookup(dep) is None

    def test_local_dep_never_matches(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(
            repo_url="_local/pkg", is_local=True, local_path="./pkg", source="local"
        )
        assert idx.lookup(dep) is None

    def test_registry_dep_never_matches(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="owner/repo", source="registry")
        assert idx.lookup(dep) is None

    def test_marketplace_dep_never_matches(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="_marketplace/mp/plugin", is_marketplace=True)
        assert idx.lookup(dep) is None

    def test_parent_inheritance_never_matches(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="_parent", is_parent_repo_inheritance=True)
        assert idx.lookup(dep) is None

    def test_duplicate_selector_rejected(self):
        rule1 = self._make_rule(repo="a/b", local_path="./x")
        rule2 = self._make_rule(repo="a/b", local_path="./y")
        with pytest.raises(ReplacementConfigError, match="Duplicate replacement selector"):
            build_replacement_index([rule1, rule2])

    def test_virtual_subpath_matches(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo", subpath="packages/base")
        idx = build_replacement_index([rule])
        dep = DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            is_virtual=True,
            virtual_path="packages/base",
        )
        assert idx.lookup(dep) is rule

    def test_subpath_mismatch(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(repo="owner/repo", subpath="packages/base")
        idx = build_replacement_index([rule])
        dep = DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            is_virtual=True,
            virtual_path="packages/other",
        )
        assert idx.lookup(dep) is None

    def test_host_mismatch(self):
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(host="gitlab.com", repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="owner/repo", host="github.com")
        assert idx.lookup(dep) is None

    def test_default_host_matches_none(self):
        """Dep with host=None defaults to github.com and matches."""
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(host="github.com", repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="owner/repo", host=None)
        assert idx.lookup(dep) is rule

    def test_github_casing_insensitive(self):
        """GitHub repo identity is case-insensitive."""
        from apm_cli.models.dependency.reference import DependencyReference

        rule = self._make_rule(host="github.com", repo="owner/repo")
        idx = build_replacement_index([rule])
        dep = DependencyReference(repo_url="Owner/Repo", host="github.com")
        assert idx.lookup(dep) is rule

    def test_rules_property(self):
        rule1 = self._make_rule(repo="a/b")
        rule2 = self._make_rule(repo="c/d")
        idx = build_replacement_index([rule1, rule2])
        assert idx.rules == [rule1, rule2]


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestReplacementSerialization:
    """Lock-file serialization and deserialization."""

    def test_local_rule_round_trip(self):
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo.git", "path": "pkg"},
                "with": {"path": "../local/pkg"},
            }
        ]
        rules = parse_replace_block(raw)
        serialized = [replacement_rule_to_dict(r) for r in rules]
        restored = replacement_rules_from_dicts(serialized)
        assert len(restored) == 1
        assert restored[0].selector.repo_url == rules[0].selector.repo_url
        assert restored[0].selector.subpath == rules[0].selector.subpath
        assert isinstance(restored[0].source, LocalReplacementSource)
        assert restored[0].source.path == "../local/pkg"

    def test_git_rule_round_trip(self):
        raw = [
            {
                "for": {"git": "https://github.com/owner/repo.git"},
                "with": {
                    "git": "https://github.com/fork/repo.git",
                    "ref": "fix",
                    "path": "sub",
                },
            }
        ]
        rules = parse_replace_block(raw)
        serialized = [replacement_rule_to_dict(r) for r in rules]
        restored = replacement_rules_from_dicts(serialized)
        assert len(restored) == 1
        src = restored[0].source
        assert isinstance(src, GitReplacementSource)
        assert src.ref == "fix"
        assert src.subpath == "sub"

    def test_serialized_shape(self):
        """Serialized dict has expected structure."""
        raw = [
            {
                "for": {"git": "https://github.com/a/b.git"},
                "with": {"path": "./local"},
            }
        ]
        rules = parse_replace_block(raw)
        d = replacement_rule_to_dict(rules[0])
        assert "for" in d
        assert "with" in d
        assert "git" in d["for"]
        assert "path" in d["with"]
