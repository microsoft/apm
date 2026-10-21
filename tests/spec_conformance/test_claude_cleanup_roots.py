"""Exercise req-tg-002 containment for installer-owned Claude cleanup writes."""

import json
from pathlib import Path

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.integration.mcp_integrator import MCPIntegrator

pytestmark = pytest.mark.component


@pytest.mark.req("req-tg-002")
def test_claude_cleanup_writes_only_to_selected_registered_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active Claude root override excludes default and other-profile writes."""
    home = tmp_path / "home"
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    payload = {
        "theme": "dark",
        "mcpServers": {
            "managed-stale": {"type": "stdio", "command": "old", "args": []},
            "user-owned": {"command": "keep"},
        },
    }
    paths = [root / ".claude.json" for root in (home, selected, other)]
    for path in paths:
        path.parent.mkdir()
        path.write_text(json.dumps(payload), encoding="utf-8")
    outside_before = {path: path.read_bytes() for path in (paths[0], paths[2])}
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected))

    MCPIntegrator.remove_stale(
        stale_names={"managed-stale"},
        runtime="claude",
        project_root=tmp_path,
        scope=InstallScope.USER,
        fail_on_write_error=True,
        stored_configs={
            "managed-stale": {
                "name": "managed-stale",
                "registry": False,
                "transport": "stdio",
                "command": "old",
            }
        },
    )

    assert {path: path.read_bytes() for path in outside_before} == outside_before
    assert json.loads(paths[1].read_text(encoding="utf-8")) == {
        "theme": "dark",
        "mcpServers": {"user-owned": {"command": "keep"}},
    }
