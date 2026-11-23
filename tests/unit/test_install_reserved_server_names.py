"""``apm install`` refuses an MCP server name that is a reserved object key.

A server name becomes a key in the client's JSON config (``.mcp.json``,
``.vscode/mcp.json``, ...). ``__proto__``, ``constructor`` and ``prototype``
are refused with a named error before anything is written, so the project
is left exactly as it was.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-INV-I7"),
    pytest.mark.promise("P-OWN-mcp-json-config-writer"),
]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "home" not in path.relative_to(root).parts
    }


@pytest.mark.parametrize("name", ["__proto__", "Constructor"])
def test_install_refuses_reserved_server_name_before_any_write(tmp_path, monkeypatch, name):
    clear_apm_yml_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home" / ".apm"))
    (tmp_path / "apm.yml").write_text(
        textwrap.dedent(
            f"""\
            name: demo
            version: 1.0.0
            dependencies:
              mcp:
                - name: {name}
                  registry: false
                  transport: stdio
                  command: echo
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "mcp.json").write_text('{"servers": {}}\n', encoding="utf-8")
    before = _snapshot(tmp_path)

    result = CliRunner().invoke(cli, ["install", "--target", "vscode", "--no-policy"])

    assert result.exit_code != 0, result.output
    flat = " ".join(result.output.split())
    assert f"Invalid MCP dependency name '{name}': reserved object key" in flat
    assert "Traceback" not in result.output
    assert _snapshot(tmp_path) == before
    clear_apm_yml_cache()
