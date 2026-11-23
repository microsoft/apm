"""Native plugin routes refuse reserved object keys as MCP and LSP server names (#4984).

The Claude and Codex native routes hand the publisher's server names to the
harness without the MCP/LSP dependency models, so they ask the same owner
(``models/dependency/server_names.py``) before admission.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.codex_plugins.client import CodexNativeError
from apm_cli.codex_plugins.manifest import (
    CODEX_COMPAT_MANIFEST,
    load_codex_compatibility_manifest,
)
from apm_cli.core.scope import InstallScope
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-claude-native-plugin-registration")]

_LSP = {"command": "pyls", "extensionToLanguage": {".py": "python"}}


def _claude_plan(tmp_path: Path, relative: str, document: dict):
    from apm_cli.claude_plugins.admission import plan_native_claude_plugin

    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "owned", "commands": []}), encoding="utf-8"
    )
    (root / relative).write_text(json.dumps(document), encoding="utf-8")
    info = PackageInfo(
        package=APMPackage(name="owned", version="1.0.0"),
        install_path=root,
        package_type=PackageType.MARKETPLACE_PLUGIN,
    )
    return plan_native_claude_plugin(
        info,
        package_name="owner/plugin",
        targets=[SimpleNamespace(name="claude")],
        scope=InstallScope.USER,
        skill_subset=None,
        admit=lambda: None,
        skip_bin=True,
    )


@pytest.mark.parametrize(
    ("relative", "document", "server"),
    [
        (".mcp.json", {"mcpServers": {"__proto__": {"command": "echo"}}}, "__proto__"),
        (".mcp.json", {"mcpServers": {"Constructor": {"command": "echo"}}}, "Constructor"),
        (".lsp.json", {"prototype": _LSP}, "prototype"),
    ],
)
def test_claude_native_route_parks_a_reserved_server_name(
    tmp_path: Path, relative: str, document: dict, server: str
) -> None:
    plan = _claude_plan(tmp_path, relative, document)

    assert plan is not None and plan.parked is not None
    reason, step = plan.parked.splitlines()
    assert reason == (
        f"Parked native plugin owner/plugin: its server name '{server}' is a reserved "
        "object key (__proto__, constructor, prototype)"
    )
    assert step.strip().startswith("Ask the publisher to rename the server")


@pytest.mark.parametrize("server", ["__proto__", "Constructor", "acme/prototype"])
def test_codex_manifest_refuses_a_reserved_mcp_server_name(tmp_path: Path, server: str) -> None:
    manifest = tmp_path / CODEX_COMPAT_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "example", "mcpServers": {server: {"command": "echo"}}}),
        encoding="utf-8",
    )

    with pytest.raises(CodexNativeError, match=rf"'{server}' is a reserved object key"):
        load_codex_compatibility_manifest(tmp_path)
