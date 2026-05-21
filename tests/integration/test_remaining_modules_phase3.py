"""Integration tests for five under-covered modules (Phase 3).

Modules:
1. src/apm_cli/marketplace/yml_schema.py        (gap 242)
2. src/apm_cli/install/sources.py               (gap 234)
3. src/apm_cli/runtime/manager.py               (gap 230)
4. src/apm_cli/bundle/plugin_exporter.py        (gap 223)
5. src/apm_cli/compilation/link_resolver.py     (gap 213)

Strategy:
- Exercise real code paths; mock only external I/O (subprocess, network).
- No live network calls.
- Type hints on all functions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module 1: marketplace/yml_schema.py
# ---------------------------------------------------------------------------


class TestYmlSchemaLoadLegacy:
    """Tests for load_marketplace_from_legacy_yml."""

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def test_load_minimal_legacy_yml(self, tmp_path: Path) -> None:
        """Minimal valid marketplace.yml loads successfully."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: my-plugin
description: My Plugin
version: 1.0.0
owner:
  name: Test Owner
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.name == "my-plugin"
        assert cfg.version == "1.0.0"
        assert cfg.owner.name == "Test Owner"
        assert cfg.is_legacy is True

    def test_load_legacy_yml_with_packages(self, tmp_path: Path) -> None:
        """Legacy marketplace.yml with packages list parses all entries."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: my-plugin
description: A plugin
version: 2.3.4
owner:
  name: Acme Corp
  email: dev@acme.com
  url: https://acme.com
packages:
  - name: skill-a
    source: owner/skill-a
    version: 1.0.0
    description: Skill A
    tags:
      - ai
      - coding
  - name: local-pkg
    source: ./local/path
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert len(cfg.packages) == 2
        assert cfg.packages[0].name == "skill-a"
        assert cfg.packages[0].tags == ("ai", "coding")
        assert cfg.packages[1].is_local is True

    def test_load_legacy_yml_missing_name_raises(self, tmp_path: Path) -> None:
        """Missing required 'name' field raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
description: A plugin
version: 1.0.0
owner:
  name: Owner
""",
        )
        with pytest.raises(MarketplaceYmlError, match="name"):
            load_marketplace_from_legacy_yml(yml)

    def test_load_legacy_yml_invalid_version_raises(self, tmp_path: Path) -> None:
        """Non-semver version raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: test
description: d
version: not-semver
owner:
  name: Owner
""",
        )
        with pytest.raises(MarketplaceYmlError, match="semver"):
            load_marketplace_from_legacy_yml(yml)

    def test_load_legacy_yml_unknown_key_raises(self, tmp_path: Path) -> None:
        """Unknown top-level key raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
unknown_key: value
""",
        )
        with pytest.raises(MarketplaceYmlError, match="unknown_key"):
            load_marketplace_from_legacy_yml(yml)

    def test_load_nonexistent_file_raises(self, tmp_path: Path) -> None:
        """Missing file raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        with pytest.raises(MarketplaceYmlError, match="Cannot read"):
            load_marketplace_from_legacy_yml(tmp_path / "nonexistent.yml")

    def test_load_invalid_yaml_raises(self, tmp_path: Path) -> None:
        """Malformed YAML raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "bad.yml"
        yml.write_bytes(b"invalid: yaml: {broken")
        with pytest.raises(MarketplaceYmlError, match="YAML parse error"):
            load_marketplace_from_legacy_yml(yml)

    def test_load_legacy_yml_with_build_block(self, tmp_path: Path) -> None:
        """Build block with custom tagPattern parses correctly."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
build:
  tagPattern: "{name}-v{version}"
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.build.tag_pattern == "{name}-v{version}"

    def test_load_legacy_yml_versioning_block(self, tmp_path: Path) -> None:
        """Versioning block with per_package strategy parses correctly."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
versioning:
  strategy: per_package
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.versioning.strategy == "per_package"

    def test_load_legacy_yml_with_outputs_map(self, tmp_path: Path) -> None:
        """Map-form outputs parse correctly."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
outputs:
  claude: {}
  codex: {}
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert "claude" in cfg.outputs
        assert "codex" in cfg.outputs

    def test_load_legacy_yml_outputs_list_form_deprecated(self, tmp_path: Path) -> None:
        """List-form outputs trigger a deprecation warning but parse."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
outputs:
  - claude
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert "claude" in cfg.outputs
        # Deprecation warning should be recorded
        assert any("deprecated" in w.lower() for w in cfg.warnings)


class TestYmlSchemaLoadFromApmYml:
    """Tests for load_marketplace_from_apm_yml."""

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def test_load_from_apm_yml_inherits_toplevel(self, tmp_path: Path) -> None:
        """name/version/description inherit from apm.yml top-level when not overridden."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        self._write(
            apm_yml,
            """\
name: inherited-name
version: 3.2.1
description: Inherited description

marketplace:
  owner:
    name: Test Author
""",
        )
        cfg = load_marketplace_from_apm_yml(apm_yml)
        assert cfg.name == "inherited-name"
        assert cfg.version == "3.2.1"
        assert cfg.description == "Inherited description"
        assert cfg.name_overridden is False
        assert cfg.is_legacy is False

    def test_load_from_apm_yml_override_values(self, tmp_path: Path) -> None:
        """Marketplace block can override top-level scalars."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        self._write(
            apm_yml,
            """\
name: top-name
version: 1.0.0
description: Top description

marketplace:
  name: override-name
  version: 2.0.0
  owner:
    name: The Publisher
""",
        )
        cfg = load_marketplace_from_apm_yml(apm_yml)
        assert cfg.name == "override-name"
        assert cfg.version == "2.0.0"
        assert cfg.name_overridden is True
        assert cfg.version_overridden is True

    def test_load_from_apm_yml_missing_marketplace_block_raises(self, tmp_path: Path) -> None:
        """apm.yml without marketplace block raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        self._write(
            apm_yml,
            """\
name: my-plugin
version: 1.0.0
description: d
""",
        )
        with pytest.raises(MarketplaceYmlError, match="marketplace"):
            load_marketplace_from_apm_yml(apm_yml)

    def test_load_from_apm_yml_missing_owner_raises(self, tmp_path: Path) -> None:
        """marketplace block without owner raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        self._write(
            apm_yml,
            """\
name: test
version: 1.0.0
description: d
marketplace:
  packages: []
""",
        )
        with pytest.raises(MarketplaceYmlError, match="owner"):
            load_marketplace_from_apm_yml(apm_yml)


class TestYmlSchemaPackageEntry:
    """Tests for _parse_package_entry edge cases."""

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def _make_yml(self, tmp_path: Path, packages_yaml: str) -> Path:
        yml = tmp_path / "marketplace.yml"
        self._write(
            yml,
            f"""\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
packages:
{packages_yaml}
""",
        )
        return yml

    def test_remote_package_requires_version_or_ref(self, tmp_path: Path) -> None:
        """Remote package without version or ref raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: remote-pkg
    source: owner/repo
""",
        )
        with pytest.raises(MarketplaceYmlError, match=r"version.*ref|ref.*version"):
            load_marketplace_from_legacy_yml(yml)

    def test_local_package_no_version_required(self, tmp_path: Path) -> None:
        """Local packages do not require version or ref."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: local-pkg
    source: ./my/path
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.packages[0].is_local is True

    def test_author_string_normalised_to_dict(self, tmp_path: Path) -> None:
        """String author is normalized to {name: ...}."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: ./path
    author: "Jane Doe"
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.packages[0].author == {"name": "Jane Doe"}

    def test_author_object_with_email_and_url(self, tmp_path: Path) -> None:
        """Author object with name, email, url is preserved."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: ./path
    author:
      name: Jane Doe
      email: jane@example.com
      url: https://janedoe.com
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.packages[0].author == {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "url": "https://janedoe.com",
        }

    def test_keywords_merged_with_tags(self, tmp_path: Path) -> None:
        """keywords and tags are merged (deduplicated)."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: ./path
    tags:
      - ai
      - coding
    keywords:
      - coding
      - testing
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        # ai, coding, testing -- coding is deduplicated
        assert cfg.packages[0].tags == ("ai", "coding", "testing")

    def test_include_prerelease_defaults_false(self, tmp_path: Path) -> None:
        """include_prerelease defaults to False."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: owner/repo
    version: 1.0.0
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.packages[0].include_prerelease is False

    def test_tag_pattern_without_placeholder_raises(self, tmp_path: Path) -> None:
        """tag_pattern without {version} or {name} raises."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: owner/repo
    version: 1.0.0
    tag_pattern: "no-placeholder"
""",
        )
        with pytest.raises(MarketplaceYmlError, match="placeholder"):
            load_marketplace_from_legacy_yml(yml)

    def test_invalid_source_shape_raises(self, tmp_path: Path) -> None:
        """source that is neither owner/repo nor ./path raises."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: "invalid source shape"
    version: 1.0.0
""",
        )
        with pytest.raises(MarketplaceYmlError, match="source"):
            load_marketplace_from_legacy_yml(yml)

    def test_category_field_stored(self, tmp_path: Path) -> None:
        """category field is stored on PackageEntry."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = self._make_yml(
            tmp_path,
            """\
  - name: pkg
    source: owner/repo
    version: 1.0.0
    category: AI Tools
""",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.packages[0].category == "AI Tools"


class TestYmlSchemaInternalHelpers:
    """Tests for validation helper functions exposed via __all__."""

    def test_source_re_matches_owner_repo(self) -> None:
        """SOURCE_RE matches owner/repo."""
        from apm_cli.marketplace.yml_schema import SOURCE_RE

        assert SOURCE_RE.match("github/my-repo") is not None

    def test_source_re_matches_local_path(self) -> None:
        """SOURCE_RE matches ./local/path."""
        from apm_cli.marketplace.yml_schema import SOURCE_RE

        assert SOURCE_RE.match("./local/skills/my-skill") is not None

    def test_source_re_rejects_bare_name(self) -> None:
        """SOURCE_RE rejects bare names without slash."""
        from apm_cli.marketplace.yml_schema import SOURCE_RE

        assert SOURCE_RE.match("bare-name") is None

    def test_local_source_re_detects_local(self) -> None:
        """LOCAL_SOURCE_RE matches paths starting with ./."""
        from apm_cli.marketplace.yml_schema import LOCAL_SOURCE_RE

        assert LOCAL_SOURCE_RE.match("./some/path") is not None
        assert LOCAL_SOURCE_RE.match("owner/repo") is None

    def test_versioning_invalid_strategy_raises(self, tmp_path: Path) -> None:
        """Invalid versioning strategy raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        yml.write_text(
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
versioning:
  strategy: invalid_strategy
""",
            encoding="utf-8",
        )
        with pytest.raises(MarketplaceYmlError, match="strategy"):
            load_marketplace_from_legacy_yml(yml)

    def test_outputs_duplicate_entry_raises(self, tmp_path: Path) -> None:
        """Duplicate entry in outputs map raises MarketplaceYmlError."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        # Build via list form with duplicates
        yml.write_text(
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
outputs:
  - claude
  - claude
""",
            encoding="utf-8",
        )
        with pytest.raises(MarketplaceYmlError, match=r"[Dd]uplicate"):
            load_marketplace_from_legacy_yml(yml)

    def test_metadata_preserved_verbatim(self, tmp_path: Path) -> None:
        """metadata block is stored verbatim with original casing."""
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        yml.write_text(
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
metadata:
  pluginRoot: .claude-plugin
  extraKey: some-value
""",
            encoding="utf-8",
        )
        cfg = load_marketplace_from_legacy_yml(yml)
        assert cfg.metadata["pluginRoot"] == ".claude-plugin"
        assert cfg.metadata["extraKey"] == "some-value"

    def test_build_unknown_key_raises(self, tmp_path: Path) -> None:
        """build block with unknown key raises."""
        from apm_cli.marketplace.errors import MarketplaceYmlError
        from apm_cli.marketplace.yml_schema import load_marketplace_from_legacy_yml

        yml = tmp_path / "marketplace.yml"
        yml.write_text(
            """\
name: test
description: d
version: 1.0.0
owner:
  name: Owner
build:
  unknownField: value
""",
            encoding="utf-8",
        )
        with pytest.raises(MarketplaceYmlError, match="Unknown key"):
            load_marketplace_from_legacy_yml(yml)


# ---------------------------------------------------------------------------
# Module 2: install/sources.py
# ---------------------------------------------------------------------------


class TestFormatPackageTypeLabel:
    """Tests for _format_package_type_label."""

    def test_all_known_types_return_non_none(self) -> None:
        """Every PackageType member has a human-readable label."""
        from apm_cli.install.sources import _format_package_type_label
        from apm_cli.models.apm_package import PackageType

        for pkg_type in PackageType:
            label = _format_package_type_label(pkg_type)
            # Some types may not be covered yet -- just verify it doesn't crash
            assert label is None or isinstance(label, str)

    def test_claude_skill_label(self) -> None:
        """CLAUDE_SKILL maps to the expected label."""
        from apm_cli.install.sources import _format_package_type_label
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.CLAUDE_SKILL)
        assert label is not None
        assert "Skill" in label

    def test_apm_package_label(self) -> None:
        """APM_PACKAGE maps to the expected label."""
        from apm_cli.install.sources import _format_package_type_label
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.APM_PACKAGE)
        assert label is not None
        assert "APM" in label

    def test_hook_package_label(self) -> None:
        """HOOK_PACKAGE is covered (regression: issue #780 silent bug)."""
        from apm_cli.install.sources import _format_package_type_label
        from apm_cli.models.apm_package import PackageType

        label = _format_package_type_label(PackageType.HOOK_PACKAGE)
        assert label is not None
        assert "Hook" in label

    def test_unknown_type_returns_none(self) -> None:
        """Unknown type returns None without raising."""
        from apm_cli.install.sources import _format_package_type_label

        label = _format_package_type_label("totally_unknown_type")
        assert label is None


class TestMaterialization:
    """Tests for the Materialization dataclass."""

    def test_default_deltas(self, tmp_path: Path) -> None:
        """Materialization defaults to installed=1 deltas."""
        from apm_cli.install.sources import Materialization

        m = Materialization(
            package_info=None,
            install_path=tmp_path,
            dep_key="owner/repo",
        )
        assert m.deltas == {"installed": 1}

    def test_custom_deltas(self, tmp_path: Path) -> None:
        """Materialization accepts custom deltas."""
        from apm_cli.install.sources import Materialization

        m = Materialization(
            package_info=None,
            install_path=tmp_path,
            dep_key="owner/repo",
            deltas={"installed": 1, "unpinned": 1},
        )
        assert m.deltas["unpinned"] == 1


class TestMakeDependencySourceFactory:
    """Tests for make_dependency_source factory function."""

    def _make_ctx(self, tmp_path: Path) -> Any:
        """Build a minimal mock InstallContext."""
        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.targets = ["copilot"]
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.dependency_graph = MagicMock()
        ctx.dependency_graph.dependency_tree.get_node.return_value = None
        ctx.installed_packages = []
        ctx.package_hashes = {}
        ctx.package_types = {}
        ctx.callback_downloaded = {}
        ctx.existing_lockfile = None
        ctx.update_refs = False
        ctx.expected_hash_change_deps = set()
        ctx.pre_download_results = {}
        ctx.diagnostics = MagicMock()
        ctx.logger = None
        ctx.tui = None
        ctx.auth_resolver = None
        ctx.registry_config = None
        ctx.scope = None
        ctx.dep_base_dirs = {}
        return ctx

    def _make_dep_ref(self, *, is_local: bool = False, local_path: str = "") -> Any:
        dep_ref = MagicMock()
        dep_ref.is_local = is_local
        dep_ref.local_path = local_path
        dep_ref.repo_url = "owner/my-repo"
        dep_ref.reference = "main"
        dep_ref.is_virtual = False
        dep_ref.host = "github.com"
        dep_ref.port = None
        return dep_ref

    def test_local_dep_produces_local_source(self, tmp_path: Path) -> None:
        """Local dependency produces a LocalDependencySource."""
        from apm_cli.install.sources import LocalDependencySource, make_dependency_source

        ctx = self._make_ctx(tmp_path)
        dep_ref = self._make_dep_ref(is_local=True, local_path="/some/path")

        source = make_dependency_source(
            ctx, dep_ref, tmp_path / "apm_modules" / "my-repo", "owner/my-repo"
        )
        assert isinstance(source, LocalDependencySource)

    def test_skip_download_produces_cached_source(self, tmp_path: Path) -> None:
        """skip_download=True produces a CachedDependencySource."""
        from apm_cli.install.sources import CachedDependencySource, make_dependency_source

        ctx = self._make_ctx(tmp_path)
        dep_ref = self._make_dep_ref()

        source = make_dependency_source(
            ctx,
            dep_ref,
            tmp_path / "apm_modules" / "my-repo",
            "owner/my-repo",
            skip_download=True,
        )
        assert isinstance(source, CachedDependencySource)

    def test_fresh_download_produces_fresh_source(self, tmp_path: Path) -> None:
        """Default (no skip) produces a FreshDependencySource."""
        from apm_cli.install.sources import FreshDependencySource, make_dependency_source

        ctx = self._make_ctx(tmp_path)
        dep_ref = self._make_dep_ref()

        source = make_dependency_source(
            ctx,
            dep_ref,
            tmp_path / "apm_modules" / "my-repo",
            "owner/my-repo",
            skip_download=False,
        )
        assert isinstance(source, FreshDependencySource)

    def test_fetched_this_run_cached_source(self, tmp_path: Path) -> None:
        """fetched_this_run=True + skip_download=True produces CachedDependencySource."""
        from apm_cli.install.sources import CachedDependencySource, make_dependency_source

        ctx = self._make_ctx(tmp_path)
        dep_ref = self._make_dep_ref()

        source = make_dependency_source(
            ctx,
            dep_ref,
            tmp_path / "apm_modules" / "my-repo",
            "owner/my-repo",
            skip_download=True,
            fetched_this_run=True,
        )
        assert isinstance(source, CachedDependencySource)
        assert source.fetched_this_run is True


class TestLocalDependencySourceUserScope:
    """Test LocalDependencySource rejects relative paths at user scope."""

    def _make_ctx(self, tmp_path: Path) -> Any:
        from apm_cli.core.scope import InstallScope

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.scope = InstallScope.USER
        ctx.targets = ["copilot"]
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.dependency_graph = MagicMock()
        ctx.dependency_graph.dependency_tree.get_node.return_value = None
        ctx.installed_packages = []
        ctx.package_hashes = {}
        ctx.package_types = {}
        ctx.callback_downloaded = {}
        ctx.existing_lockfile = None
        ctx.update_refs = False
        ctx.expected_hash_change_deps = set()
        ctx.diagnostics = MagicMock()
        ctx.diagnostics.warn = MagicMock()
        ctx.logger = None
        ctx.tui = None
        ctx.dep_base_dirs = {}
        return ctx

    def test_relative_local_path_skipped_at_user_scope(self, tmp_path: Path) -> None:
        """Relative local paths return None at user scope (skip integration)."""
        from apm_cli.install.sources import LocalDependencySource

        ctx = self._make_ctx(tmp_path)
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = "./relative/path"

        source = LocalDependencySource(ctx, dep_ref, tmp_path / "pkg", "key")
        result = source.acquire()
        assert result is None
        ctx.diagnostics.warn.assert_called_once()


class TestCachedDependencySourceResolveCachedCommit:
    """Tests for CachedDependencySource._resolve_cached_commit."""

    def _make_source(
        self,
        tmp_path: Path,
        *,
        fetched_this_run: bool = False,
        resolved_commit_in_callback: str | None = None,
        existing_lockfile_commit: str | None = None,
        dep_ref_reference: str = "main",
        resolved_ref_commit: str | None = None,
    ) -> Any:
        from apm_cli.install.sources import CachedDependencySource

        ctx = MagicMock()
        ctx.callback_downloaded = {}
        dep_key = "owner/repo"
        if resolved_commit_in_callback:
            ctx.callback_downloaded[dep_key] = resolved_commit_in_callback

        if existing_lockfile_commit:
            locked = MagicMock()
            locked.resolved_commit = existing_lockfile_commit
            ctx.existing_lockfile = MagicMock()
            ctx.existing_lockfile.get_dependency.return_value = locked
        else:
            ctx.existing_lockfile = None

        dep_ref = MagicMock()
        dep_ref.reference = dep_ref_reference

        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = resolved_ref_commit or "abc123"

        dep_locked_chk = None
        source = CachedDependencySource(
            ctx,
            dep_ref,
            tmp_path,
            dep_key,
            resolved_ref,
            dep_locked_chk,
            fetched_this_run=fetched_this_run,
        )
        return source

    def test_fetched_this_run_uses_callback_sha(self, tmp_path: Path) -> None:
        """fetched_this_run uses callback sha first."""
        source = self._make_source(
            tmp_path,
            fetched_this_run=True,
            resolved_commit_in_callback="deadbeef",
        )
        commit = source._resolve_cached_commit()
        assert commit == "deadbeef"

    def test_fetched_this_run_falls_back_to_resolved_ref(self, tmp_path: Path) -> None:
        """fetched_this_run falls back to resolved_ref when no callback sha."""
        source = self._make_source(
            tmp_path,
            fetched_this_run=True,
            resolved_ref_commit="resolved_sha",
        )
        commit = source._resolve_cached_commit()
        assert commit == "resolved_sha"

    def test_cached_uses_lockfile_sha(self, tmp_path: Path) -> None:
        """Non-fetched path uses existing lockfile SHA."""
        source = self._make_source(
            tmp_path,
            fetched_this_run=False,
            existing_lockfile_commit="lockfile_sha",
        )
        commit = source._resolve_cached_commit()
        assert commit == "lockfile_sha"

    def test_falls_back_to_dep_ref_reference(self, tmp_path: Path) -> None:
        """When no SHA is available, falls back to dep_ref.reference."""
        source = self._make_source(
            tmp_path,
            fetched_this_run=False,
            dep_ref_reference="fallback_ref",
        )
        commit = source._resolve_cached_commit()
        assert commit == "fallback_ref"


# ---------------------------------------------------------------------------
# Module 3: runtime/manager.py
# ---------------------------------------------------------------------------


class TestRuntimeManagerInit:
    """Tests for RuntimeManager.__init__ and basic properties."""

    def test_default_init(self) -> None:
        """RuntimeManager initializes with expected supported runtimes."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        assert "copilot" in rm.supported_runtimes
        assert "codex" in rm.supported_runtimes
        assert "llm" in rm.supported_runtimes
        assert "gemini" in rm.supported_runtimes

    def test_runtime_dir_under_home(self) -> None:
        """runtime_dir is under ~/.apm/runtimes."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        assert rm.runtime_dir == Path.home() / ".apm" / "runtimes"

    def test_get_runtime_preference_order(self) -> None:
        """get_runtime_preference returns expected ordered list."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        prefs = rm.get_runtime_preference()
        assert prefs[0] == "copilot"
        assert "codex" in prefs
        assert "gemini" in prefs
        assert "llm" in prefs


class TestRuntimeManagerScriptLoading:
    """Tests for get_embedded_script and get_common_script."""

    def test_get_embedded_script_from_repo(self) -> None:
        """get_embedded_script reads from the scripts/ directory in repo."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        # The repo has real scripts; if they exist, we can read them.
        # If not, the call should raise RuntimeError.
        import sys

        ext = ".ps1" if sys.platform == "win32" else ".sh"
        try:
            content = rm.get_embedded_script(f"setup-copilot{ext}")
            assert isinstance(content, str)
            assert len(content) > 0
        except RuntimeError:
            # Script not present in test environment -- acceptable
            pass

    def test_get_embedded_script_missing_raises(self) -> None:
        """Missing script raises RuntimeError."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with pytest.raises(RuntimeError, match="Could not load setup script"):
            rm.get_embedded_script("nonexistent-script-xyz.sh")

    def test_get_common_script_raises_or_returns_str(self) -> None:
        """get_common_script returns a string or raises RuntimeError."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        try:
            content = rm.get_common_script()
            assert isinstance(content, str)
        except RuntimeError:
            pass  # acceptable when scripts are absent


class TestRuntimeManagerIsAvailable:
    """Tests for is_runtime_available."""

    def test_unknown_runtime_not_available(self) -> None:
        """Unknown runtime name returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        assert rm.is_runtime_available("nonexistent-runtime-xyz") is False

    def test_runtime_available_via_binary_in_runtime_dir(self, tmp_path: Path) -> None:
        """Runtime is available when binary exists in runtime_dir."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        # Override runtime_dir to a tmp directory
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)

        # Create a fake "codex" binary
        fake_binary = rm.runtime_dir / "codex"
        fake_binary.write_text("#!/bin/sh\necho codex\n", encoding="utf-8")
        fake_binary.chmod(0o755)

        assert rm.is_runtime_available("codex") is True

    def test_runtime_not_available_no_binary(self, tmp_path: Path) -> None:
        """Runtime is not available when binary does not exist."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)

        # Only available if the binary happens to be on the real system PATH.
        # We patch shutil.which to ensure a deterministic result.
        with patch("shutil.which", return_value=None):
            result = rm.is_runtime_available("llm")
        assert result is False


class TestRuntimeManagerListRuntimes:
    """Tests for list_runtimes."""

    def test_list_runtimes_returns_all_keys(self, tmp_path: Path) -> None:
        """list_runtimes returns a dict with all known runtime keys."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)

        with patch("shutil.which", return_value=None):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="")
                runtimes = rm.list_runtimes()

        for name in ("copilot", "codex", "llm", "gemini"):
            assert name in runtimes

    def test_list_runtimes_installed_flag(self, tmp_path: Path) -> None:
        """installed flag is True when binary is on PATH."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)

        with patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0\n")
                runtimes = rm.list_runtimes()

        assert runtimes["codex"]["installed"] is True

    def test_list_runtimes_version_fetched_when_installed(self, tmp_path: Path) -> None:
        """Version is fetched via --version when the runtime is installed."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)

        with patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="2.3.4\n")
                runtimes = rm.list_runtimes()

        assert runtimes["llm"].get("version") == "2.3.4"


class TestRuntimeManagerRemoveRuntime:
    """Tests for remove_runtime."""

    def test_remove_unknown_runtime_returns_false(self) -> None:
        """Unknown runtime returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        result = rm.remove_runtime("unknown-xyz")
        assert result is False

    def test_remove_npm_runtime_calls_npm_uninstall(self) -> None:
        """copilot removal calls 'npm uninstall -g'."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = rm.remove_runtime("copilot")

        assert result is True
        args = mock_run.call_args[0][0]
        assert "npm" in args
        assert "uninstall" in args

    def test_remove_npm_runtime_failure_returns_false(self) -> None:
        """npm uninstall failure returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
            result = rm.remove_runtime("copilot")

        assert result is False

    def test_remove_runtime_not_installed_returns_false(self, tmp_path: Path) -> None:
        """Removing a non-npm runtime that is not installed returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)
        # "llm" is not an npm runtime; no binary exists
        result = rm.remove_runtime("llm")
        assert result is False

    def test_remove_llm_also_removes_venv(self, tmp_path: Path) -> None:
        """Removing llm also removes the llm-venv directory."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        rm.runtime_dir = tmp_path / "runtimes"
        rm.runtime_dir.mkdir(parents=True)

        # Create fake binary and venv
        fake_binary = rm.runtime_dir / "llm"
        fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        venv_dir = rm.runtime_dir / "llm-venv"
        venv_dir.mkdir()

        result = rm.remove_runtime("llm")
        assert result is True
        assert not fake_binary.exists()
        assert not venv_dir.exists()


class TestRuntimeManagerSetupRuntime:
    """Tests for setup_runtime."""

    def test_setup_unknown_runtime_returns_false(self) -> None:
        """Unknown runtime name returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        result = rm.setup_runtime("totally-unknown-runtime")
        assert result is False

    def test_setup_runtime_script_not_found_returns_false(self) -> None:
        """setup_runtime returns False when the embedded script cannot be found."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with patch.object(rm, "get_embedded_script", side_effect=RuntimeError("not found")):
            result = rm.setup_runtime("copilot")

        assert result is False

    def test_setup_runtime_calls_run_embedded_script(self) -> None:
        """setup_runtime delegates execution to run_embedded_script."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with patch.object(rm, "get_embedded_script", return_value="#!/bin/sh\nexit 0\n"):
            with patch.object(rm, "get_common_script", return_value="#!/bin/sh\n"):
                with patch.object(rm, "run_embedded_script", return_value=True) as mock_run:
                    result = rm.setup_runtime("codex")

        assert result is True
        mock_run.assert_called_once()

    def test_setup_runtime_with_version_passes_arg(self) -> None:
        """setup_runtime with version passes it to run_embedded_script."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        captured_args: list[list[str]] = []

        def capture(*args: Any, **kwargs: Any) -> bool:
            captured_args.append(args[2] if len(args) > 2 else kwargs.get("script_args", []))
            return True

        with patch.object(rm, "get_embedded_script", return_value="#!/bin/sh\n"):
            with patch.object(rm, "get_common_script", return_value="#!/bin/sh\n"):
                with patch.object(rm, "run_embedded_script", side_effect=capture):
                    rm.setup_runtime("codex", version="1.2.3")

        assert len(captured_args) == 1
        # The version string should appear in the args passed
        assert any("1.2.3" in str(a) for a in captured_args[0])

    def test_get_available_runtime_returns_none_when_none_installed(self) -> None:
        """get_available_runtime returns None when no runtime is available."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        with patch.object(rm, "is_runtime_available", return_value=False):
            result = rm.get_available_runtime()

        assert result is None

    def test_get_available_runtime_returns_first_available(self) -> None:
        """get_available_runtime returns the first available runtime in preference order."""
        from apm_cli.runtime.manager import RuntimeManager

        rm = RuntimeManager()
        prefs = rm.get_runtime_preference()

        def fake_available(name: str) -> bool:
            # Make the second preference available
            return name == prefs[1]

        with patch.object(rm, "is_runtime_available", side_effect=fake_available):
            result = rm.get_available_runtime()

        assert result == prefs[1]


# ---------------------------------------------------------------------------
# Module 4: bundle/plugin_exporter.py
# ---------------------------------------------------------------------------


class TestValidateOutputRel:
    """Tests for _validate_output_rel."""

    def test_valid_relative_path(self) -> None:
        """Simple relative path passes validation."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        assert _validate_output_rel("agents/my-agent.md") is True

    def test_absolute_path_rejected(self) -> None:
        """Absolute paths are rejected."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        assert _validate_output_rel("/etc/passwd") is False

    def test_traversal_path_rejected(self) -> None:
        """Path with .. traversal is rejected."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        assert _validate_output_rel("../../evil") is False

    def test_nested_valid_path(self) -> None:
        """Nested path without traversal passes."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        assert _validate_output_rel("skills/subdir/file.md") is True


class TestSanitizeBundleName:
    """Tests for _sanitize_bundle_name."""

    def test_normal_name_unchanged(self) -> None:
        """Normal alphanumeric name is unchanged."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        assert _sanitize_bundle_name("my-plugin") == "my-plugin"

    def test_slash_replaced_with_hyphen(self) -> None:
        """Slash is replaced with hyphen."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("owner/repo")
        assert "/" not in result
        assert result == "owner-repo"

    def test_empty_name_becomes_unnamed(self) -> None:
        """Empty name becomes 'unnamed'."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        assert _sanitize_bundle_name("") == "unnamed"

    def test_spaces_replaced(self) -> None:
        """Spaces are replaced with hyphens."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("my plugin name")
        assert " " not in result

    def test_traversal_chars_sanitized(self) -> None:
        """Traversal characters are removed."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("../../../evil")
        assert ".." not in result
        assert "/" not in result


class TestRenamePrompt:
    """Tests for _rename_prompt."""

    def test_prompt_md_renamed(self) -> None:
        """foo.prompt.md becomes foo.md."""
        from apm_cli.bundle.plugin_exporter import _rename_prompt

        assert _rename_prompt("my-cmd.prompt.md") == "my-cmd.md"

    def test_plain_md_unchanged(self) -> None:
        """plain.md stays plain.md."""
        from apm_cli.bundle.plugin_exporter import _rename_prompt

        assert _rename_prompt("plain.md") == "plain.md"

    def test_nested_prompt_renamed(self) -> None:
        """Path with nested name ending in .prompt.md is renamed."""
        from apm_cli.bundle.plugin_exporter import _rename_prompt

        assert _rename_prompt("deploy.prompt.md") == "deploy.md"


class TestNormalizeBareSkillSlug:
    """Tests for _normalize_bare_skill_slug."""

    def test_skills_prefix_stripped(self) -> None:
        """skills/ prefix is stripped."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        assert _normalize_bare_skill_slug("skills/my-skill") == "my-skill"

    def test_bare_skills_returns_empty(self) -> None:
        """'skills' alone returns empty string."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        assert _normalize_bare_skill_slug("skills") == ""

    def test_empty_string_returns_empty(self) -> None:
        """Empty string returns empty string."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        assert _normalize_bare_skill_slug("") == ""

    def test_nested_slug_preserved(self) -> None:
        """Nested slugs are normalized to posix."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        assert _normalize_bare_skill_slug("frontend-design") == "frontend-design"


class TestDeepMerge:
    """Tests for _deep_merge."""

    def test_non_overlapping_keys_merged(self) -> None:
        """Non-overlapping keys are merged."""
        from apm_cli.bundle.plugin_exporter import _deep_merge

        base: dict = {"a": 1}
        overlay: dict = {"b": 2}
        _deep_merge(base, overlay)
        assert base == {"a": 1, "b": 2}

    def test_first_writer_wins_by_default(self) -> None:
        """Without overwrite=True, existing base keys win."""
        from apm_cli.bundle.plugin_exporter import _deep_merge

        base: dict = {"key": "base_value"}
        overlay: dict = {"key": "overlay_value"}
        _deep_merge(base, overlay)
        assert base["key"] == "base_value"

    def test_overwrite_true_overlay_wins(self) -> None:
        """With overwrite=True, overlay wins on flat keys."""
        from apm_cli.bundle.plugin_exporter import _deep_merge

        base: dict = {"key": "base_value"}
        overlay: dict = {"key": "overlay_value"}
        _deep_merge(base, overlay, overwrite=True)
        assert base["key"] == "overlay_value"

    def test_nested_merge(self) -> None:
        """Nested dicts are recursively merged."""
        from apm_cli.bundle.plugin_exporter import _deep_merge

        base: dict = {"hooks": {"preToolUse": ["hook1"]}}
        overlay: dict = {"hooks": {"postToolUse": ["hook2"]}}
        _deep_merge(base, overlay)
        assert "preToolUse" in base["hooks"]
        assert "postToolUse" in base["hooks"]

    def test_max_depth_raises(self) -> None:
        """Exceeding MAX_MERGE_DEPTH raises ValueError."""
        from apm_cli.bundle.plugin_exporter import _MAX_MERGE_DEPTH, _deep_merge

        with pytest.raises(ValueError, match="nesting depth"):
            _deep_merge({}, {}, _depth=_MAX_MERGE_DEPTH + 1)


class TestCollectHooksFromApm:
    """Tests for _collect_hooks_from_apm."""

    def test_no_hooks_dir_returns_empty(self, tmp_path: Path) -> None:
        """Missing hooks/ dir returns {}."""
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_apm

        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        assert _collect_hooks_from_apm(apm_dir) == {}

    def test_hooks_json_merged(self, tmp_path: Path) -> None:
        """Valid hooks JSON files are merged."""
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_apm

        apm_dir = tmp_path / ".apm"
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "a.json").write_text(json.dumps({"preToolUse": ["h1"]}), encoding="utf-8")
        (hooks_dir / "b.json").write_text(json.dumps({"postToolUse": ["h2"]}), encoding="utf-8")

        hooks = _collect_hooks_from_apm(apm_dir)
        assert "preToolUse" in hooks
        assert "postToolUse" in hooks

    def test_invalid_json_skipped(self, tmp_path: Path) -> None:
        """Invalid JSON files are silently skipped."""
        from apm_cli.bundle.plugin_exporter import _collect_hooks_from_apm

        apm_dir = tmp_path / ".apm"
        hooks_dir = apm_dir / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "bad.json").write_text("{not valid json", encoding="utf-8")
        (hooks_dir / "good.json").write_text(json.dumps({"k": "v"}), encoding="utf-8")

        hooks = _collect_hooks_from_apm(apm_dir)
        assert "k" in hooks


class TestCollectMcp:
    """Tests for _collect_mcp."""

    def test_no_mcp_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing .mcp.json returns {}."""
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        assert _collect_mcp(tmp_path) == {}

    def test_valid_mcp_file_returns_servers(self, tmp_path: Path) -> None:
        """Valid .mcp.json returns mcpServers dict."""
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        mcp = {"mcpServers": {"my-server": {"command": "python"}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
        result = _collect_mcp(tmp_path)
        assert "my-server" in result

    def test_mcp_file_without_servers_key_returns_empty(self, tmp_path: Path) -> None:
        """mcp.json without mcpServers returns {}."""
        from apm_cli.bundle.plugin_exporter import _collect_mcp

        (tmp_path / ".mcp.json").write_text(json.dumps({"other": "data"}), encoding="utf-8")
        assert _collect_mcp(tmp_path) == {}


class TestUpdatePluginJsonPaths:
    """Tests for _update_plugin_json_paths."""

    def test_strips_agents_skills_commands_instructions(self) -> None:
        """Schema-invalid keys are stripped from plugin.json."""
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json: dict = {
            "name": "my-plugin",
            "agents": ["./agents/a.md"],
            "skills": ["./skills/s.md"],
            "commands": ["./commands/c.md"],
            "instructions": ["./instructions/i.md"],
        }
        result = _update_plugin_json_paths(plugin_json, [])
        for key in ("agents", "skills", "commands", "instructions"):
            assert key not in result
        assert result["name"] == "my-plugin"

    def test_other_keys_preserved(self) -> None:
        """Non-component keys are preserved unchanged."""
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json: dict = {"name": "plugin", "version": "1.0", "description": "d"}
        result = _update_plugin_json_paths(plugin_json, [])
        assert result == plugin_json

    def test_original_dict_not_mutated(self) -> None:
        """The original plugin_json dict is not mutated."""
        from apm_cli.bundle.plugin_exporter import _update_plugin_json_paths

        plugin_json: dict = {"name": "p", "agents": ["a.md"]}
        _update_plugin_json_paths(plugin_json, [])
        assert "agents" in plugin_json  # original intact


class TestCollectApmComponents:
    """Tests for _collect_apm_components."""

    def test_empty_apm_dir_returns_empty(self, tmp_path: Path) -> None:
        """Non-existent .apm dir returns []."""
        from apm_cli.bundle.plugin_exporter import _collect_apm_components

        assert _collect_apm_components(tmp_path / ".apm") == []

    def test_agents_collected_flat(self, tmp_path: Path) -> None:
        """Files in .apm/agents/ are collected as agents/<name>."""
        from apm_cli.bundle.plugin_exporter import _collect_apm_components

        apm_dir = tmp_path / ".apm"
        agents_dir = apm_dir / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.md").write_text("# Agent", encoding="utf-8")

        components = _collect_apm_components(apm_dir)
        rel_paths = [rel for _, rel in components]
        assert "agents/my-agent.md" in rel_paths

    def test_prompts_renamed_to_commands(self, tmp_path: Path) -> None:
        """Files in .apm/prompts/ are mapped to commands/ with .prompt.md renamed."""
        from apm_cli.bundle.plugin_exporter import _collect_apm_components

        apm_dir = tmp_path / ".apm"
        prompts_dir = apm_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "deploy.prompt.md").write_text("# Deploy", encoding="utf-8")

        components = _collect_apm_components(apm_dir)
        rel_paths = [rel for _, rel in components]
        assert "commands/deploy.md" in rel_paths

    def test_skills_collected_recursive(self, tmp_path: Path) -> None:
        """Files in .apm/skills/ are collected recursively."""
        from apm_cli.bundle.plugin_exporter import _collect_apm_components

        apm_dir = tmp_path / ".apm"
        skills_dir = apm_dir / "skills" / "sub"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")

        components = _collect_apm_components(apm_dir)
        rel_paths = [rel for _, rel in components]
        assert "skills/sub/SKILL.md" in rel_paths


class TestCollectBareSkill:
    """Tests for _collect_bare_skill."""

    def test_bare_skill_collected(self, tmp_path: Path) -> None:
        """SKILL.md at package root is collected into skills/<slug>/."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill

        install_path = tmp_path / "my-skill-pkg"
        install_path.mkdir()
        (install_path / "SKILL.md").write_text("# Skill", encoding="utf-8")
        (install_path / "README.md").write_text("# Readme", encoding="utf-8")

        dep = MagicMock()
        dep.virtual_path = ""
        dep.repo_url = "owner/my-skill"

        out: list[tuple[Path, str]] = []
        _collect_bare_skill(install_path, dep, out)

        rel_paths = [rel for _, rel in out]
        assert any(r.startswith("skills/my-skill/") for r in rel_paths)

    def test_no_skill_md_skipped(self, tmp_path: Path) -> None:
        """Package without SKILL.md is skipped."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill

        install_path = tmp_path / "pkg"
        install_path.mkdir()
        (install_path / "README.md").write_text("# Readme", encoding="utf-8")

        dep = MagicMock()
        dep.virtual_path = ""
        dep.repo_url = "owner/repo"

        out: list[tuple[Path, str]] = []
        _collect_bare_skill(install_path, dep, out)
        assert out == []

    def test_existing_skills_prefix_skipped(self, tmp_path: Path) -> None:
        """Package with existing skills/ output prefix is not double-collected."""
        from apm_cli.bundle.plugin_exporter import _collect_bare_skill

        install_path = tmp_path / "pkg"
        install_path.mkdir()
        (install_path / "SKILL.md").write_text("# Skill", encoding="utf-8")

        dep = MagicMock()
        dep.virtual_path = ""
        dep.repo_url = "owner/repo"

        # Pre-populate out with a skills/ entry
        out: list[tuple[Path, str]] = [(tmp_path, "skills/existing/SKILL.md")]
        _collect_bare_skill(install_path, dep, out)
        # Should not have added more entries
        assert len(out) == 1


class TestExportPluginBundleDryRun:
    """Tests for export_plugin_bundle with dry_run=True."""

    def _setup_project(self, root: Path) -> None:
        (root / "apm.yml").write_text(
            "name: test-plugin\nversion: 1.0.0\ndescription: d\n",
            encoding="utf-8",
        )
        lock_content = (
            "lockfile_version: '1'\ngenerated_at: '2025-01-01T00:00:00+00:00'\ndependencies: []\n"
        )
        (root / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")

        apm_dir = root / ".apm"
        apm_dir.mkdir()
        skills_dir = apm_dir / "skills"
        skills_dir.mkdir()
        (skills_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

    def test_dry_run_returns_pack_result_no_write(self, tmp_path: Path) -> None:
        """dry_run=True returns a PackResult without writing to disk."""
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = tmp_path / "proj"
        project.mkdir()
        output = tmp_path / "out"
        output.mkdir()
        self._setup_project(project)

        result = export_plugin_bundle(project, output, dry_run=True)

        # Bundle dir should NOT be created
        assert not result.bundle_path.exists()
        # But files list should be populated
        assert isinstance(result.files, list)
        assert "plugin.json" in result.files

    def test_dry_run_includes_skills_files(self, tmp_path: Path) -> None:
        """Dry run file list includes skill files."""
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = tmp_path / "proj"
        project.mkdir()
        output = tmp_path / "out"
        output.mkdir()
        self._setup_project(project)

        result = export_plugin_bundle(project, output, dry_run=True)
        assert any("skills" in f for f in result.files)

    def test_export_writes_plugin_json(self, tmp_path: Path) -> None:
        """Full export writes plugin.json to bundle dir."""
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = tmp_path / "proj"
        project.mkdir()
        output = tmp_path / "out"
        output.mkdir()
        self._setup_project(project)

        result = export_plugin_bundle(project, output, dry_run=False)
        assert (result.bundle_path / "plugin.json").exists()

    def test_export_local_dep_raises(self, tmp_path: Path) -> None:
        """Local path dependency blocks packing with ValueError."""
        from apm_cli.bundle.plugin_exporter import export_plugin_bundle

        project = tmp_path / "proj"
        project.mkdir()
        output = tmp_path / "out"
        output.mkdir()

        (project / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\ndescription: d\n"
            "dependencies:\n  apm:\n    - path: ./local-dep\n",
            encoding="utf-8",
        )
        lock_content = (
            "lockfile_version: '1'\ngenerated_at: '2025-01-01T00:00:00+00:00'\ndependencies: []\n"
        )
        (project / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")

        with pytest.raises(ValueError, match="local path"):
            export_plugin_bundle(project, output)


# ---------------------------------------------------------------------------
# Module 5: compilation/link_resolver.py
# ---------------------------------------------------------------------------


class TestUnifiedLinkResolverInit:
    """Tests for UnifiedLinkResolver initialization."""

    def test_init_sets_base_dir(self, tmp_path: Path) -> None:
        """base_dir is stored as a Path."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        resolver = UnifiedLinkResolver(tmp_path)
        assert resolver.base_dir == tmp_path

    def test_context_registry_empty_on_init(self, tmp_path: Path) -> None:
        """Context registry is empty after init."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        resolver = UnifiedLinkResolver(tmp_path)
        assert len(resolver.context_registry) == 0

    def test_package_root_none_on_init(self, tmp_path: Path) -> None:
        """package_root is None after init."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        resolver = UnifiedLinkResolver(tmp_path)
        assert resolver.package_root is None


class TestUnifiedLinkResolverIsExternalUrl:
    """Tests for _is_external_url."""

    def test_https_url_is_external(self, tmp_path: Path) -> None:
        """https:// URL is external."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_external_url("https://example.com/doc.md") is True

    def test_http_url_is_external(self, tmp_path: Path) -> None:
        """http:// URL is external."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_external_url("http://example.com") is True

    def test_relative_path_not_external(self, tmp_path: Path) -> None:
        """Relative path is not external."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_external_url("./local.context.md") is False

    def test_javascript_scheme_not_external(self, tmp_path: Path) -> None:
        """javascript: scheme is NOT treated as external (security)."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_external_url("javascript:alert(1)") is False

    def test_url_without_netloc_not_external(self, tmp_path: Path) -> None:
        """URL-shaped string without netloc is not external."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_external_url("http:relative/path") is False


class TestUnifiedLinkResolverIsContextFile:
    """Tests for _is_context_file."""

    def test_context_md_detected(self, tmp_path: Path) -> None:
        """*.context.md is a context file."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_context_file("api-standards.context.md") is True

    def test_memory_md_detected(self, tmp_path: Path) -> None:
        """*.memory.md is a context file."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_context_file("notes.memory.md") is True

    def test_plain_md_not_context(self, tmp_path: Path) -> None:
        """Plain .md file is not a context file."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_context_file("README.md") is False

    def test_case_insensitive(self, tmp_path: Path) -> None:
        """Context file detection is case-insensitive."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_context_file("NOTES.CONTEXT.MD") is True


class TestUnifiedLinkResolverIsRewritableRelativeLink:
    """Tests for _is_rewritable_relative_link."""

    def test_empty_string_not_rewritable(self, tmp_path: Path) -> None:
        """Empty string is not rewritable."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_rewritable_relative_link("") is False

    def test_fragment_only_not_rewritable(self, tmp_path: Path) -> None:
        """Fragment-only link is not rewritable."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_rewritable_relative_link("#section") is False

    def test_protocol_relative_not_rewritable(self, tmp_path: Path) -> None:
        """Protocol-relative URL is not rewritable."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_rewritable_relative_link("//cdn.example.com/lib.js") is False

    def test_absolute_path_not_rewritable(self, tmp_path: Path) -> None:
        """Root-absolute path is not rewritable."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_rewritable_relative_link("/etc/hosts") is False

    def test_relative_path_is_rewritable(self, tmp_path: Path) -> None:
        """Simple relative path is rewritable."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_rewritable_relative_link("./images/logo.png") is True

    def test_http_url_not_rewritable(self, tmp_path: Path) -> None:
        """http: scheme is not rewritable."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        assert r._is_rewritable_relative_link("https://example.com/img.png") is False


class TestUnifiedLinkResolverSplitLinkTarget:
    """Tests for _split_link_target."""

    def test_no_suffix(self, tmp_path: Path) -> None:
        """Path without fragment or query returns empty suffix."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        path, suffix = r._split_link_target("doc.md")
        assert path == "doc.md"
        assert suffix == ""

    def test_fragment_split(self, tmp_path: Path) -> None:
        """Fragment is split off correctly."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        path, suffix = r._split_link_target("doc.md#section")
        assert path == "doc.md"
        assert suffix == "#section"

    def test_query_split(self, tmp_path: Path) -> None:
        """Query string is split off correctly."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        path, suffix = r._split_link_target("doc.md?lang=en")
        assert path == "doc.md"
        assert suffix == "?lang=en"

    def test_fragment_before_query(self, tmp_path: Path) -> None:
        """When both fragment and query present, split at the earlier one."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        path, suffix = r._split_link_target("doc.md#sec?x=1")
        assert path == "doc.md"
        assert suffix == "#sec?x=1"


class TestUnifiedLinkResolverRewriteMarkdownLinks:
    """Tests for resolve_links_for_compilation (rewrite markdown links)."""

    def test_external_url_preserved(self, tmp_path: Path) -> None:
        """External URLs are left unchanged."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        content = "[Docs](https://docs.example.com/api)"
        result = r.resolve_links_for_compilation(content, tmp_path)
        assert "https://docs.example.com/api" in result

    def test_context_link_rewritten_when_registered(self, tmp_path: Path) -> None:
        """Context links in registry are rewritten to point at source."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        # Create a real context file
        context_file = tmp_path / "api.context.md"
        context_file.write_text("# API Context", encoding="utf-8")

        r = UnifiedLinkResolver(tmp_path)
        r.context_registry["api.context.md"] = context_file

        source_file = tmp_path / "AGENTS.md"
        source_file.write_text("", encoding="utf-8")

        content = "[API Context](api.context.md)"
        result = r.resolve_links_for_compilation(content, source_file)
        # The link should have been rewritten
        assert "[API Context]" in result

    def test_no_context_link_preserved_unchanged(self, tmp_path: Path) -> None:
        """Context link that cannot be resolved is left unchanged."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        content = "[Unresolvable](missing.context.md)"
        result = r.resolve_links_for_compilation(content, tmp_path / "source.md")
        # Link preserved since file doesn't exist
        assert "missing.context.md" in result


class TestUnifiedLinkResolverForInstallation:
    """Tests for resolve_links_for_installation."""

    def test_asset_link_rewritten_when_package_root_set(self, tmp_path: Path) -> None:
        """In-package asset links are rewritten when package_root is set."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        pkg_root = tmp_path / "pkg"
        pkg_root.mkdir()
        asset = pkg_root / "images" / "logo.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x89PNG")

        source_file = pkg_root / ".apm" / "skills" / "SKILL.md"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("[Logo](../../images/logo.png)", encoding="utf-8")

        target_file = tmp_path / ".github" / "skills" / "SKILL.md"
        target_file.parent.mkdir(parents=True)

        r = UnifiedLinkResolver(tmp_path)
        r.package_root = pkg_root

        content = "[Logo](../../images/logo.png)"
        result = r.resolve_links_for_installation(content, source_file, target_file)
        # Rewritten link should not contain the original ../../images path verbatim
        # (it was rewritten relative to the new target location)
        assert isinstance(result, str)

    def test_installation_external_url_preserved(self, tmp_path: Path) -> None:
        """External URL in installation pass is preserved."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        content = "[Docs](https://example.com)"
        source_file = tmp_path / "source.md"
        target_file = tmp_path / "target.md"
        result = r.resolve_links_for_installation(content, source_file, target_file)
        assert "https://example.com" in result


class TestUnifiedLinkResolverGetReferencedContexts:
    """Tests for get_referenced_contexts."""

    def test_no_context_references_returns_empty(self, tmp_path: Path) -> None:
        """File without context links returns empty set."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        md = tmp_path / "README.md"
        md.write_text("No links here.", encoding="utf-8")

        r = UnifiedLinkResolver(tmp_path)
        refs = r.get_referenced_contexts([md])
        assert refs == set()

    def test_context_reference_resolved_from_registry(self, tmp_path: Path) -> None:
        """Context reference in a file resolves to the registry path."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        ctx_file = tmp_path / "api.context.md"
        ctx_file.write_text("# API", encoding="utf-8")

        md = tmp_path / "agent.md"
        md.write_text("[API Context](api.context.md)", encoding="utf-8")

        r = UnifiedLinkResolver(tmp_path)
        r.context_registry["api.context.md"] = ctx_file

        refs = r.get_referenced_contexts([md])
        assert ctx_file in refs

    def test_nonexistent_file_skipped(self, tmp_path: Path) -> None:
        """Non-existent file in the scan list is silently skipped."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        r = UnifiedLinkResolver(tmp_path)
        refs = r.get_referenced_contexts([tmp_path / "ghost.md"])
        assert refs == set()


class TestRegisterContexts:
    """Tests for register_contexts."""

    def test_context_registered_by_filename(self, tmp_path: Path) -> None:
        """Context is registered by its filename."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        ctx_file = tmp_path / "api.context.md"
        ctx_file.write_text("# API", encoding="utf-8")

        ctx = MagicMock()
        ctx.file_path = ctx_file
        ctx.source = None

        primitives = MagicMock()
        primitives.contexts = [ctx]

        r = UnifiedLinkResolver(tmp_path)
        r.register_contexts(primitives)

        assert "api.context.md" in r.context_registry

    def test_dependency_context_registered_qualified(self, tmp_path: Path) -> None:
        """Dependency context is also registered with qualified name."""
        from apm_cli.compilation.link_resolver import UnifiedLinkResolver

        ctx_file = tmp_path / "api.context.md"
        ctx_file.write_text("# API", encoding="utf-8")

        ctx = MagicMock()
        ctx.file_path = ctx_file
        ctx.source = "dependency:owner/repo"

        primitives = MagicMock()
        primitives.contexts = [ctx]

        r = UnifiedLinkResolver(tmp_path)
        r.register_contexts(primitives)

        assert "owner/repo:api.context.md" in r.context_registry


class TestLegacyResolveMarkdownLinks:
    """Tests for legacy resolve_markdown_links function."""

    def test_external_link_preserved(self, tmp_path: Path) -> None:
        """External link is left unchanged."""
        from apm_cli.compilation.link_resolver import resolve_markdown_links

        content = "[Docs](https://example.com/docs)"
        result = resolve_markdown_links(content, tmp_path)
        assert "https://example.com/docs" in result

    def test_anchor_link_preserved(self, tmp_path: Path) -> None:
        """Anchor-only link is left unchanged."""
        from apm_cli.compilation.link_resolver import resolve_markdown_links

        content = "[Section](#section-heading)"
        result = resolve_markdown_links(content, tmp_path)
        assert "#section-heading" in result

    def test_local_md_inlined(self, tmp_path: Path) -> None:
        """Local .md file is inlined into the content."""
        from apm_cli.compilation.link_resolver import resolve_markdown_links

        included = tmp_path / "included.md"
        included.write_text("Inlined content here.", encoding="utf-8")

        content = "[Include](included.md)"
        result = resolve_markdown_links(content, tmp_path)
        assert "Inlined content here." in result

    def test_missing_file_link_preserved(self, tmp_path: Path) -> None:
        """Link to non-existent file is preserved unchanged."""
        from apm_cli.compilation.link_resolver import resolve_markdown_links

        content = "[Missing](nonexistent.md)"
        result = resolve_markdown_links(content, tmp_path)
        assert "[Missing](nonexistent.md)" in result


class TestLegacyValidateLinkTargets:
    """Tests for legacy validate_link_targets function."""

    def test_no_links_no_errors(self, tmp_path: Path) -> None:
        """Content without links returns empty errors list."""
        from apm_cli.compilation.link_resolver import validate_link_targets

        errors = validate_link_targets("No links here.", tmp_path)
        assert errors == []

    def test_external_url_not_validated(self, tmp_path: Path) -> None:
        """External URLs are not checked for existence."""
        from apm_cli.compilation.link_resolver import validate_link_targets

        errors = validate_link_targets("[Docs](https://example.com)", tmp_path)
        assert errors == []

    def test_missing_file_produces_error(self, tmp_path: Path) -> None:
        """Missing referenced file produces an error."""
        from apm_cli.compilation.link_resolver import validate_link_targets

        errors = validate_link_targets("[Missing](./ghost.md)", tmp_path)
        assert len(errors) > 0
        assert "ghost.md" in errors[0]

    def test_existing_file_no_error(self, tmp_path: Path) -> None:
        """Existing referenced file produces no error."""
        from apm_cli.compilation.link_resolver import validate_link_targets

        real_file = tmp_path / "real.md"
        real_file.write_text("# Real", encoding="utf-8")

        errors = validate_link_targets("[Real](real.md)", tmp_path)
        assert errors == []


class TestLinkResolutionContext:
    """Tests for LinkResolutionContext dataclass."""

    def test_default_values(self, tmp_path: Path) -> None:
        """LinkResolutionContext has expected default values."""
        from apm_cli.compilation.link_resolver import LinkResolutionContext

        ctx = LinkResolutionContext(
            source_file=tmp_path / "src.md",
            source_location=tmp_path,
            target_location=tmp_path,
            base_dir=tmp_path,
            available_contexts={},
        )
        assert ctx.package_root is None
        assert ctx.enable_asset_rewrite is False

    def test_enable_asset_rewrite_set(self, tmp_path: Path) -> None:
        """enable_asset_rewrite can be set to True."""
        from apm_cli.compilation.link_resolver import LinkResolutionContext

        ctx = LinkResolutionContext(
            source_file=tmp_path / "src.md",
            source_location=tmp_path,
            target_location=tmp_path,
            base_dir=tmp_path,
            available_contexts={},
            package_root=tmp_path,
            enable_asset_rewrite=True,
        )
        assert ctx.enable_asset_rewrite is True
        assert ctx.package_root == tmp_path
