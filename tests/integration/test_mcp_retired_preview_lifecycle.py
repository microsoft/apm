"""``install --dry-run`` previews the retired MCP heal the real install makes (#3967).

A 0.36.0 project whose ``vscode`` alias wrote ``.vscode/mcp.json`` is previewed
and then installed. The preview says "move to .mcp.json" only for a server
Copilot rewrites there; a server no longer declared, or written by another
target, is previewed as a removal from the retired file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain, http_server

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0361" / "t2" / "v0.36.0"
_MOVE = "MCP servers that would move from .vscode/mcp.json to .mcp.json:"
_REMOVE = "MCP servers that would be removed from .vscode/mcp.json:"


def _section(output: str, header: str) -> list[str]:
    """Server names listed under *header* in the dry-run output."""
    lines = [line.strip() for line in output.splitlines()]
    names: list[str] = []
    for line in lines[lines.index(f"[i] {header}") + 1 :] if f"[i] {header}" in lines else ():
        if not line.startswith("[i]   - "):
            break
        names.append(line.removeprefix("[i]   - "))
    return names


@pytest.mark.parametrize(
    ("variant", "moved", "removed", "root", "cursor"),
    [
        pytest.param("unchanged", ["srv"], [], {"srv"}, set(), id="unchanged"),
        pytest.param("replace", [], ["srv"], {"replacement"}, set(), id="replace"),
        pytest.param("cursor", [], ["srv"], set(), {"srv"}, id="retarget-cursor"),
    ],
)
def test_dry_run_preview_matches_the_retired_heal(
    tmp_path: Path,
    apm_binary_path: Path,
    variant: str,
    moved: list[str],
    removed: list[str],
    root: set[str],
    cursor: set[str],
) -> None:
    chain = Chain(tmp_path / "world", apm_binary_path)
    shutil.copytree(_FIXTURE / "pkg", chain.isolated.work_root / "pkg")
    shutil.copytree(_FIXTURE / "plural-alias", chain.project, dirs_exist_ok=True)
    manifest_path = chain.project / "apm.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if variant == "replace":
        manifest["dependencies"]["mcp"] = [http_server("replacement", "https://example.invalid/m")]
    if variant == "cursor":
        manifest["targets"] = ["cursor"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    preview = chain.ok("install", "--dry-run", "--no-policy")
    output = preview.stdout + preview.stderr
    assert _section(output, _MOVE) == moved
    assert _section(output, _REMOVE) == removed
    assert set(chain.servers("copilot")) == set()

    chain.ok("install", "--no-policy")
    assert not (chain.project / ".vscode" / "mcp.json").exists()
    assert set(chain.servers("copilot")) == root
    assert set(chain.servers("cursor")) == cursor
