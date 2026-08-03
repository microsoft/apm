"""Lifecycle coverage for plugin collection marketplace version checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]


def _write_plugin_only_marketplace(project_root: Path) -> None:
    """Create a marketplace with a plugin.json-only local package."""
    (project_root / "apm.yml").write_text(
        """\
name: plugin-marketplace
description: Plugin collection version check fixture.
version: "1.2.3"
marketplace:
  name: plugin-marketplace
  description: Plugin collection version check fixture.
  version: "1.2.3"
  owner:
    name: APM
  versioning:
    strategy: tag_pattern
  build:
    tagPattern: "{name}-v{version}"
  outputs:
    claude: {}
  packages:
    - name: my-plugin
      source: ./plugins/my-plugin
      version: "1.2.3"
""",
        encoding="utf-8",
    )
    plugin_dir = project_root / "plugins" / "my-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "my-plugin", "version": "1.2.3"}),
        encoding="utf-8",
    )


def test_plugin_local_version_check_dry_run_is_aligned_without_build_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin-only local packages pass the JSON release gate without writing output."""
    _write_plugin_only_marketplace(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["pack", "--check-versions", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["version_alignment"] == {
        "strategy": "tag_pattern",
        "expected": None,
        "ok": True,
        "packages": [
            {
                "path": "plugins/my-plugin",
                "version": "1.2.3",
                "ok": True,
                "reason": "matches",
            }
        ],
    }
    assert payload["bundle"] is None
    assert payload["plugin_manifests"]["written"] == []
    assert not (tmp_path / "build").exists()
