"""Integration tests for the unified ``apm pack`` entrypoint.

Covers the matrix of bundle / marketplace / both / neither outputs plus
flag overrides and the hard-error path for the removed
``apm marketplace build`` subcommand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml  # noqa: F401
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.commands.pack import pack_cmd
from apm_cli.models.validation import PackageType, validate_apm_package

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LOCKFILE_TEMPLATE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""


def _write_apm_yml(root: Path, body: str) -> None:
    (root / "apm.yml").write_text(body, encoding="utf-8")


def _write_minimal_lockfile(root: Path) -> None:
    """Write an empty but well-formed apm.lock.yaml so pack_bundle works."""
    (root / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")


def _write_marketplace_block_yml(root: Path, *, package_name: str = "azure") -> None:
    """Write an apm.yml with a marketplace block targeting a local source."""
    plugin_dir = root / ".github" / "plugins" / package_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    _write_apm_yml(
        root,
        f"""\
name: pack-test
version: 1.0.0
description: pack integration test fixture

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: {package_name}
      description: Local package fixture for pack integration tests
      source: ./.github/plugins/{package_name}
      homepage: https://example.com
""",
    )


def _write_agent_block_yml(root: Path, *, nonportable: bool = True) -> None:
    _write_apm_yml(
        root,
        """\
name: agent-pack
version: 1.0.0
description: agent plugin fixture
dependencies:
  apm: []
""",
    )
    _write_minimal_lockfile(root)
    lockfile = root / "apm.lock.yaml"
    lockfile.write_text(
        _LOCKFILE_TEMPLATE
        + "mcp_servers: [safe]\n"
        + "mcp_configs:\n"
        + "  safe:\n"
        + "    name: safe\n"
        + "    transport: stdio\n"
        + "    command: tool\n"
        + (
            "lsp_servers: [pyright]\n"
            "lsp_configs:\n"
            "  pyright:\n"
            "    name: pyright\n"
            "    command: pyright-langserver\n"
            "    extensionToLanguage:\n"
            "      .py: python\n"
            if nonportable
            else ""
        ),
        encoding="utf-8",
    )
    (root / ".apm" / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (root / ".apm" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n\nUse the demo skill.\n",
        encoding="utf-8",
    )
    if nonportable:
        for directory, name, content in (
            ("agents", "agent.md", "agent"),
            ("commands", "hello.md", "command"),
            ("instructions", "note.md", "note"),
            ("extensions", "ext.json", "{}"),
        ):
            component_dir = root / ".apm" / directory
            component_dir.mkdir(parents=True, exist_ok=True)
            (component_dir / name).write_text(content, encoding="utf-8")
        (root / ".apm" / "hooks").mkdir(parents=True, exist_ok=True)
        (root / ".apm" / "hooks" / "hooks.json").write_text(
            json.dumps({"preCommit": [{"command": "lint"}]}),
            encoding="utf-8",
        )
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"safe": {"type": "stdio", "command": "tool"}}}),
            encoding="utf-8",
        )
        (root / ".lsp.json").write_text(
            json.dumps({"lspServers": {"pyright": {"command": "pyright-langserver"}}}),
            encoding="utf-8",
        )
    for name in ("README.md", "LICENSE", "CHANGELOG.md"):
        (root / name).write_text(name, encoding="utf-8")


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPackUnified:
    def test_pack_bundle_only(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n",
        )
        _write_minimal_lockfile(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        # Bundle directory is created under ./build (empty bundle is fine)
        assert (tmp_path / "build").exists()
        # Marketplace.json should NOT be created
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_defaults_to_legacy_claude_plugin(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        bundle = next((tmp_path / "build").iterdir())
        manifest = json.loads((bundle / "plugin.json").read_text(encoding="utf-8"))
        assert "$schema" not in manifest
        assert (bundle / "agents" / "agent.md").exists()
        assert (bundle / ".mcp.json").exists()
        assert not (bundle / "com.microsoft.apm").exists()

    def test_pack_default_succeeds_without_lockfile(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: no-lock\nversion: 1.0.0\ndescription: No lockfile fixture\ntarget: claude\n",
        )

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        manifest = json.loads(
            (tmp_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert "$schema" not in manifest

    def test_pack_explicit_agent_plugin_round_trips(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path, nonportable=False)

        result = runner.invoke(pack_cmd, ["--format", "agent-plugin"])

        assert result.exit_code == 0, result.output
        bundle = next((tmp_path / "build").iterdir())
        assert (bundle / "plugin.json").exists()
        assert (bundle / "skills" / "demo" / "SKILL.md").exists()
        assert (bundle / "mcp.json").exists()
        assert not (bundle / "com.microsoft.apm").exists()
        assert not (bundle / "agents").exists()
        assert not (bundle / "commands").exists()
        assert not (bundle / "instructions").exists()
        assert not (bundle / "extensions").exists()
        assert not (bundle / "hooks").exists()
        assert not (bundle / "lsp.json").exists()
        assert (bundle / "README.md").exists()
        assert (bundle / "LICENSE").exists()
        assert (bundle / "CHANGELOG.md").exists()
        assert not (bundle / "apm.yml").exists()
        assert not (bundle / "apm.yaml").exists()

    def test_pack_explicit_agent_plugin_rejects_nonportable_sources(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--format", "agent-plugin"])

        assert result.exit_code == 1
        assert "non-portable primitives would be discarded" in result.output
        assert "apm pack --format claude-plugin" in result.output
        assert "carried by neither 'agent-plugin' nor 'claude-plugin'" in result.output
        assert not (tmp_path / "build").exists()

    def test_pack_json_does_not_claim_default_flip_before_t10(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("apm_cli.version.get_version", lambda: "0.30.0")
        _write_agent_block_yml(tmp_path, nonportable=False)

        result = runner.invoke(pack_cmd, ["--format", "agent-plugin", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert not any(
            "defaults to Agent Plugin output" in warning for warning in payload["warnings"]
        )

    def test_pack_json_reports_nonportable_agent_plugin_failure(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--format", "agent-plugin", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["errors"][0]["code"] == "build_error"
        assert "non-portable primitives would be discarded" in payload["errors"][0]["message"]
        assert not (tmp_path / "build").exists()

    def test_pack_claude_plugin_preserves_legacy_layout(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--claude-plugin"])

        assert result.exit_code == 0, result.output
        bundle = next((tmp_path / "build").iterdir())
        assert (bundle / "plugin.json").exists()
        assert (bundle / "agents" / "agent.md").exists()
        assert not (bundle / "com.microsoft.apm" / "agents" / "agent.md").exists()
        validation = validate_apm_package(bundle)
        assert validation.is_valid is True
        assert validation.package_type is PackageType.MARKETPLACE_PLUGIN

    def test_pack_plugin_alias_preserves_legacy_layout(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--format", "plugin"])

        assert result.exit_code == 0, result.output
        bundle = next((tmp_path / "build").iterdir())
        manifest = json.loads((bundle / "plugin.json").read_text(encoding="utf-8"))
        assert "$schema" not in manifest
        assert (bundle / "agents" / "agent.md").exists()

    def test_pack_conflicting_format_selectors_error(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_agent_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--format", "agent-plugin", "--claude-plugin"])

        assert result.exit_code == 2
        assert "Choose one bundle format selector" in result.output

    def test_pack_marketplace_only(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        out = tmp_path / ".claude-plugin" / "marketplace.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["name"] == "pack-test"
        assert data["plugins"][0]["name"] == "azure"
        # No bundle directory should appear
        assert not (tmp_path / "build").exists()

    def test_pack_marketplace_writes_codex_when_selected(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)
        apm = tmp_path / "apm.yml"
        apm.write_text(
            apm.read_text(encoding="utf-8")
            .replace(
                "  owner:\n",
                "  outputs: [claude, codex]\n  owner:\n",
                1,
            )
            .replace(
                "      homepage: https://example.com\n",
                "      homepage: https://example.com\n      category: Productivity\n",
                1,
            ),
            encoding="utf-8",
        )

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        claude_out = tmp_path / ".claude-plugin" / "marketplace.json"
        codex_out = tmp_path / ".agents" / "plugins" / "marketplace.json"
        assert claude_out.exists()
        assert codex_out.exists()
        data = json.loads(codex_out.read_text(encoding="utf-8"))
        assert data["plugins"][0]["source"] == {
            "source": "local",
            "path": "./.github/plugins/azure",
        }

    def test_pack_marketplace_uses_configured_output_paths(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)
        apm = tmp_path / "apm.yml"
        apm.write_text(
            apm.read_text(encoding="utf-8")
            .replace(
                "  owner:\n",
                "  outputs: [claude, codex]\n"
                "  claude:\n"
                "    output: dist/claude-marketplace.json\n"
                "  codex:\n"
                "    output: dist/codex-marketplace.json\n"
                "  owner:\n",
                1,
            )
            .replace(
                "      homepage: https://example.com\n",
                "      homepage: https://example.com\n      category: Productivity\n",
                1,
            ),
            encoding="utf-8",
        )

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "dist" / "claude-marketplace.json").exists()
        assert (tmp_path / "dist" / "codex-marketplace.json").exists()
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()
        assert not (tmp_path / ".agents" / "plugins" / "marketplace.json").exists()

    def test_pack_marketplace_output_override_only_affects_claude_in_dual_output_config(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)
        apm = tmp_path / "apm.yml"
        apm.write_text(
            apm.read_text(encoding="utf-8")
            .replace(
                "  owner:\n",
                "  outputs: [claude, codex]\n"
                "  codex:\n"
                "    output: dist/codex-marketplace.json\n"
                "  owner:\n",
                1,
            )
            .replace(
                "      homepage: https://example.com\n",
                "      homepage: https://example.com\n      category: Productivity\n",
                1,
            ),
            encoding="utf-8",
        )
        claude_override = tmp_path / "dist" / "legacy-override.json"

        result = runner.invoke(pack_cmd, ["--marketplace-path", f"claude={claude_override}"])

        assert result.exit_code == 0, result.output
        assert claude_override.exists()
        assert (tmp_path / "dist" / "codex-marketplace.json").exists()
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_rejects_codex_output_traversal(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)
        apm = tmp_path / "apm.yml"
        apm.write_text(
            apm.read_text(encoding="utf-8")
            .replace(
                "  owner:\n",
                "  outputs: [codex]\n"
                "  codex:\n"
                "    output: ../../outside-marketplace.json\n"
                "  owner:\n",
                1,
            )
            .replace(
                "      homepage: https://example.com\n",
                "      homepage: https://example.com\n      category: Productivity\n",
                1,
            ),
            encoding="utf-8",
        )

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 1
        assert "traversal" in result.output.lower()
        assert not (tmp_path.parent.parent / "outside-marketplace.json").exists()

    def test_pack_both(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Add both blocks
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: pack-test
version: 1.0.0
description: y
dependencies:
  apm: []

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: azure
      description: x
      source: ./.github/plugins/azure
""",
        )
        _write_minimal_lockfile(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "build").exists()
        assert (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_neither_errors(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: x\nversion: 0.1.0\ndescription: y\n")

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 1
        assert "Nothing to pack" in result.output

    def test_pack_marketplace_output_override(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        out_path = tmp_path / "out" / "m.json"
        result = runner.invoke(pack_cmd, ["--marketplace-path", f"claude={out_path}"])

        assert result.exit_code == 0, result.output
        assert out_path.exists()
        # Default location should NOT be written when overridden
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_legacy_marketplace_yml(self, runner, tmp_path, monkeypatch):
        """Legacy standalone marketplace.yml still produces marketplace.json."""
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        # apm.yml has neither dependencies nor marketplace blocks
        _write_apm_yml(tmp_path, "name: x\nversion: 0.1.0\ndescription: y\n")
        (tmp_path / "marketplace.yml").write_text(
            """\
name: legacy
version: 0.1.0
description: y
owner:
  name: Tester
  url: https://example.com
packages:
  - name: azure
    description: x
    source: ./.github/plugins/azure
""",
            encoding="utf-8",
        )

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        # Legacy default path is ./marketplace.json (kept by yml_schema)
        assert (tmp_path / "marketplace.json").exists()
        # Deprecation warning should fire
        assert "marketplace.yml" in result.output.lower()

    def test_pack_dry_run_marketplace(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--dry-run"])

        assert result.exit_code == 0, result.output
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_plugin_format_with_marketplace(self, runner, tmp_path, monkeypatch):
        """--format plugin still triggers marketplace producer."""
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: pack-test
version: 1.0.0
description: y
dependencies:
  apm: []

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: azure
      description: x
      source: ./.github/plugins/azure
""",
        )
        _write_minimal_lockfile(tmp_path)

        result = runner.invoke(pack_cmd, ["--format", "plugin"])

        assert result.exit_code == 0, result.output
        # Marketplace.json must be written regardless of bundle format
        assert (tmp_path / ".claude-plugin" / "marketplace.json").exists()


# ---------------------------------------------------------------------------
# Removed `apm marketplace build` subcommand
# ---------------------------------------------------------------------------


class TestMarketplaceBuildSubcommandRemoved:
    def test_marketplace_build_subcommand_errors(self, runner):
        result = runner.invoke(marketplace, ["build"])
        # Click maps UsageError to exit code 2.
        assert result.exit_code == 2
        assert "apm pack" in result.output
        assert "was removed" in result.output


# ---------------------------------------------------------------------------
# --check-clean drift recipe (issue #1381 acceptance criterion)
# ---------------------------------------------------------------------------


class TestCheckCleanDriftRecipe:
    """Integration tests: --check-clean drift detection emits the recovery recipe."""

    def test_drift_recipe_contains_amend_and_force_with_lease(self, runner, tmp_path, monkeypatch):
        """Missing marketplace.json triggers exit 4 with both recovery incantations.

        Issue #1381 AC: the error text must contain both 'commit --amend' and
        'force-with-lease' so producers can recover without consulting external docs.
        """
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline"])

        assert result.exit_code == 4, f"Expected exit 4, got {result.exit_code}:\n{result.output}"
        assert "commit --amend" in result.output, (
            "--check-clean drift output must include the amend recipe"
        )
        assert "force-with-lease" in result.output, (
            "--check-clean drift output must include the force-with-lease recipe"
        )

    def test_drift_recipe_embeds_marketplace_json_path(self, runner, tmp_path, monkeypatch):
        """The git add line in the recipe must embed the marketplace.json path."""
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline"])

        assert result.exit_code == 4
        # Verify the git add -- <path> line embeds the path so producers can copy-paste.
        assert "git add" in result.output
        assert "marketplace.json" in result.output

    def test_drift_recipe_preserves_marketplace_path_override(self, runner, tmp_path, monkeypatch):
        """The recovery command must regenerate the same overridden path it stages."""
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)
        override = "claude=.github/plugin/marketplace.json"

        result = runner.invoke(
            pack_cmd,
            ["--check-clean", "--dry-run", "--offline", "--marketplace-path", override],
        )

        assert result.exit_code == 4, result.output
        assert f"apm pack --marketplace-path {override}" in result.output
        assert "git add -- .github/plugin/marketplace.json" in result.output


class TestCheckCleanEffectiveOutputPath:
    """Integration tests: --check-clean uses pack's effective output path."""

    def test_check_clean_uses_marketplace_path_override(self, runner, tmp_path, monkeypatch):
        """The clean gate must compare the artifact selected by the CLI override."""
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)
        artifact = tmp_path / ".github" / "plugin" / "marketplace.json"
        override = "claude=.github/plugin/marketplace.json"

        packed = runner.invoke(pack_cmd, ["--offline", "--marketplace-path", override])

        assert packed.exit_code == 0, packed.output
        assert artifact.is_file()
        initial_bytes = artifact.read_bytes()
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

        checked = runner.invoke(
            pack_cmd,
            ["--offline", "--marketplace-path", override, "--check-clean"],
        )

        assert checked.exit_code == 0, checked.output
        assert ".github/plugin/marketplace.json  [unchanged]" in checked.output
        assert artifact.read_bytes() == initial_bytes
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()
