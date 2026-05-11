"""Tests for ``load_marketplace_from_apm_yml``.

Covers inheritance of name/description/version from the apm.yml top
level, override semantics inside the marketplace block, and rejection
of unknown keys within the marketplace block.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS, known_output_names
from apm_cli.marketplace.yml_schema import load_marketplace_from_apm_yml


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def test_marketplace_output_profiles_define_supported_outputs() -> None:
    assert known_output_names() == {"claude", "codex"}
    assert MARKETPLACE_OUTPUTS["claude"].mapper == "claude"
    assert MARKETPLACE_OUTPUTS["claude"].supports_cli_output_override is True
    assert MARKETPLACE_OUTPUTS["codex"].mapper == "codex"
    assert MARKETPLACE_OUTPUTS["codex"].supports_cli_output_override is False
    assert MARKETPLACE_OUTPUTS["codex"].required_package_fields == ("category",)


_MIN_BLOCK_INHERIT = """\
name: my-project
description: Project description.
version: 1.2.3
marketplace:
  owner:
    name: ACME
  packages:
    - name: tool-a
      source: acme/tool-a
      ref: v1.0.0
"""


_MIN_BLOCK_OVERRIDE = """\
name: my-project
description: Project description.
version: 1.2.3
marketplace:
  name: my-marketplace
  description: A separate marketplace.
  version: 9.9.9
  owner:
    name: ACME
  packages:
    - name: tool-a
      source: acme/tool-a
      ref: v1.0.0
"""


class TestInheritance:
    def test_name_description_version_inherited(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_INHERIT)
        config = load_marketplace_from_apm_yml(apm)
        assert config.name == "my-project"
        assert config.description == "Project description."
        assert config.version == "1.2.3"
        assert config.is_legacy is False
        assert config.name_overridden is False
        assert config.description_overridden is False
        assert config.version_overridden is False

    def test_overrides_take_precedence(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_OVERRIDE)
        config = load_marketplace_from_apm_yml(apm)
        assert config.name == "my-marketplace"
        assert config.description == "A separate marketplace."
        assert config.version == "9.9.9"
        assert config.name_overridden is True
        assert config.description_overridden is True
        assert config.version_overridden is True

    def test_default_output_is_claude_plugin(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_INHERIT)
        config = load_marketplace_from_apm_yml(apm)
        assert config.output == ".claude-plugin/marketplace.json"
        assert config.outputs == ("claude",)
        assert config.claude.output == ".claude-plugin/marketplace.json"

    def test_outputs_list_parsed(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.2.3
            marketplace:
              owner:
                name: ACME
              outputs: [claude, codex]
              packages: []
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.outputs == ("claude", "codex")

    def test_outputs_scalar_parsed(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.2.3
            marketplace:
              owner:
                name: ACME
              outputs: codex
              packages: []
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.outputs == ("codex",)

    def test_claude_block_parsed(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.2.3
            marketplace:
              owner:
                name: ACME
              outputs: [codex]
              claude:
                output: build/claude-marketplace.json
              codex:
                output: build/codex-marketplace.json
              packages: []
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.outputs == ("codex",)
        assert config.claude.output == "build/claude-marketplace.json"
        assert config.output == "build/claude-marketplace.json"

    def test_top_level_output_remains_claude_shorthand(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.2.3
            marketplace:
              owner:
                name: ACME
              output: build/legacy-output.json
              packages: []
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.claude.output == "build/legacy-output.json"
        assert config.output == "build/legacy-output.json"

    def test_claude_block_wins_over_top_level_output(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.2.3
            marketplace:
              owner:
                name: ACME
              output: build/legacy-output.json
              claude:
                output: build/explicit-claude.json
              packages: []
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.claude.output == "build/explicit-claude.json"
        assert config.output == "build/explicit-claude.json"

    def test_codex_defaults_disabled(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, _MIN_BLOCK_INHERIT)
        config = load_marketplace_from_apm_yml(apm)
        assert config.outputs == ("claude",)
        assert config.codex.output == ".agents/plugins/marketplace.json"

    def test_codex_block_parsed(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.2.3
            marketplace:
              owner:
                name: ACME
              outputs: [claude, codex]
              codex:
                output: .agents/plugins/marketplace.json
              packages: []
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.outputs == ("claude", "codex")
        assert config.codex.output == ".agents/plugins/marketplace.json"


class TestValidation:
    def test_missing_marketplace_block_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(apm, "name: foo\nversion: 1.0.0\n")
        with pytest.raises(MarketplaceYmlError, match="marketplace"):
            load_marketplace_from_apm_yml(apm)

    def test_unknown_key_in_block_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            marketplace:
              owner:
                name: A
              bogus: 1
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match="bogus"):
            load_marketplace_from_apm_yml(apm)

    def test_missing_owner_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            description: x
            marketplace:
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match="owner"):
            load_marketplace_from_apm_yml(apm)

    def test_codex_unknown_key_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              codex:
                nope: true
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match="nope"):
            load_marketplace_from_apm_yml(apm)

    def test_codex_enabled_key_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              codex:
                enabled: true
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match=r"enabled"):
            load_marketplace_from_apm_yml(apm)

    def test_claude_enabled_key_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              claude:
                enabled: true
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match=r"enabled"):
            load_marketplace_from_apm_yml(apm)

    def test_outputs_must_not_be_empty(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              outputs: []
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match="at least one marketplace output"):
            load_marketplace_from_apm_yml(apm)

    def test_unknown_output_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              outputs: [claude, cursor]
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match="Unknown marketplace output 'cursor'"):
            load_marketplace_from_apm_yml(apm)

    def test_duplicate_output_rejected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              outputs: [claude, claude]
              packages: []
            """,
        )
        with pytest.raises(MarketplaceYmlError, match="Duplicate marketplace output 'claude'"):
            load_marketplace_from_apm_yml(apm)

    def test_codex_block_not_required_when_codex_output_selected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              outputs: [codex]
              packages:
                - name: local-tool
                  source: ./plugins/local-tool
                  category: Productivity
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.outputs == ("codex",)
        assert config.codex.output == ".agents/plugins/marketplace.json"

    def test_package_category_required_when_codex_output_selected(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              outputs: [codex]
              packages:
                - name: local-tool
                  source: ./plugins/local-tool
            """,
        )
        with pytest.raises(MarketplaceYmlError, match=r"category"):
            load_marketplace_from_apm_yml(apm)

    def test_package_category_parsed(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: foo
            version: 1.0.0
            marketplace:
              owner:
                name: A
              outputs: [codex]
              packages:
                - name: local-tool
                  source: ./plugins/local-tool
                  category: Developer Tools
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        assert config.packages[0].category == "Developer Tools"


class TestLocalPackages:
    def test_local_source_skips_version_requirement(self, tmp_path: Path) -> None:
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            """\
            name: my-project
            version: 1.0.0
            marketplace:
              owner:
                name: A
              packages:
                - name: local-tool
                  source: ./packages/local-tool
            """,
        )
        config = load_marketplace_from_apm_yml(apm)
        pkg = config.packages[0]
        assert pkg.is_local is True
        assert pkg.source == "./packages/local-tool"
        assert pkg.version is None
        assert pkg.ref is None
