"""Integration coverage for ``apm mcp export``."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from apm_cli.commands.mcp import mcp


def test_export_writes_vscode_config_from_self_defined_dep(tmp_path):
    """The export command materializes real runtime config without install."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("apm.yml").write_text(
            """\
name: export-fixture
version: 0.1.0
targets:
  - copilot
dependencies:
  mcp:
    - name: my-server
      registry: false
      transport: stdio
      command: python
      args:
        - -m
        - my_server
""",
            encoding="utf-8",
        )

        result = runner.invoke(mcp, ["export", "--runtime", "vscode"])

        assert result.exit_code == 0, result.output
        mcp_config = json.loads(Path(".vscode/mcp.json").read_text(encoding="utf-8"))
        server = mcp_config["servers"]["my-server"]
        assert server["command"] == "python"
        assert server["args"] == ["-m", "my_server"]
