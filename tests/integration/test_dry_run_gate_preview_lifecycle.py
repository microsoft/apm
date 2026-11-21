"""``apm install --dry-run`` previews the gate removal the real install performs (#4454).

After ``apm deny <pkg>``, the dry-run names each executable the next install
retires and writes nothing; the install that follows then retires them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain, stdio_server
from tests.integration._policy_scope_kit import tree_snapshot

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-prospective-dry-run-install-plan"),
]

_PACKAGE = "./pkgs/tools"


def _text(result) -> str:
    return f"{result.stdout}\n{result.stderr}"


def test_dry_run_previews_the_gate_removal_the_install_performs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = Chain(tmp_path / "world", apm_binary_path, dirs=(".claude",))
    package = chain.project / "pkgs" / "tools"
    package.mkdir(parents=True)
    manifest = {
        "name": "tools",
        "version": "1.0.0",
        "dependencies": {"mcp": [stdio_server("tools-server", "x")]},
    }
    (package / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    chain.write_manifest([], apm=[_PACKAGE], targets=["claude"])
    chain.ok("install", "--no-policy")
    assert "tools-server" in chain.servers("claude")
    chain.ok("deny", _PACKAGE)
    before = tree_snapshot(chain.project)

    preview = chain.ok("install", "--dry-run", "--no-policy")

    lines = preview.stdout.splitlines()
    header = "[i] Executables that would be retired (decision changed since apm.lock.yaml): 1"
    assert header in lines, _text(preview)
    assert f"[i]   - {_PACKAGE} is denied locally" in lines, _text(preview)
    assert "[i]       mcp (denied): tools-server" in lines, _text(preview)
    assert tree_snapshot(chain.project) == before, "dry-run changed state"

    chain.ok("install", "--no-policy")
    assert "tools-server" not in chain.servers("claude")
