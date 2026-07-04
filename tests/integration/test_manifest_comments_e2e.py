"""E2E regression test for comment-preserving apm.yml rewrites."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_e2e_mode,
]

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


@pytest.fixture(autouse=True)
def _clear_manifest_cache() -> None:
    """Keep manifest parsing isolated across CLI invocations."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def test_install_preserves_manifest_comments_and_formatting(tmp_path, monkeypatch) -> None:
    """Running real ``apm install`` keeps comments and formatting in apm.yml."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_PROGRESS", "never")
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        """\
# project intent: keep this manifest annotated
name: comment-roundtrip
version: "1.0.0"

dependencies:
  # APM dependency notes must survive installs
  apm:
    - "owner/existing-skill" # inline package note
  mcp: []  # compact MCP list stays compact
targets:
  # target comment remains attached
  - copilot
""",
        encoding="utf-8",
    )

    install_result = SimpleNamespace(installed_count=1, diagnostics=None)
    runner = CliRunner()

    with (
        patch(_PATCH_UPDATES, return_value=None),
        patch("apm_cli.commands.install._validate_package_exists", return_value=True),
        patch("apm_cli.commands.install._install_apm_dependencies", return_value=install_result),
    ):
        result = runner.invoke(
            cli,
            ["install", "owner/new-skill", "--only=apm", "--no-policy"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    rendered = manifest.read_text(encoding="utf-8")
    parsed = yaml.safe_load(rendered)

    assert parsed["dependencies"]["apm"] == [
        "owner/existing-skill",
        "owner/new-skill",
    ]
    assert "# project intent: keep this manifest annotated" in rendered
    assert 'version: "1.0.0"' in rendered
    assert "  # APM dependency notes must survive installs" in rendered
    assert '    - "owner/existing-skill" # inline package note' in rendered
    assert "  mcp: []  # compact MCP list stays compact" in rendered
    assert "  # target comment remains attached" in rendered
