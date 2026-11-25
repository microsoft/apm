"""Live CLI evidence behind generator definition rules (installed-CLI, e2e).

The generator's rule checks in test_generator_rules.py run in-process; a rule
whose evidence needs the real CLI proves it here instead.
"""

from __future__ import annotations

import pytest
import yaml

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-executable-item-admission"),
]


def test_approve_never_scans_a_root_declared_mcp_server(lifecycle_world) -> None:
    """R5 evidence for U-FIRST-PARTY-MCP: ``apm approve`` / ``apm deny`` decide only the
    installed packages ``scan_installed_executable_packages`` finds, so approve-deny of an
    MCP source maps onto the first-party rule cell. Fails if approve ever governs root MCP.
    """
    from apm_cli.commands.approve import scan_installed_executable_packages
    from tests.lifecycle import driver
    from tests.lifecycle.world import use_policy
    from tests.lifecycle.world.packages import INERT_COMMAND

    use_policy(lifecycle_world, "allow")
    manifest = lifecycle_world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    root = {"name": "p-root", "registry": False, "transport": "stdio", "command": INERT_COMMAND}
    data["dependencies"] = {"apm": [lifecycle_world.packages["p-exec"].spec], "mcp": [root]}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = driver.run(lifecycle_world, ("install",))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "p-root" in (lifecycle_world.project / ".mcp.json").read_text(encoding="utf-8")

    declarations = scan_installed_executable_packages(manifest)
    assert [d.package_name for d in declarations if d.mcp_count] == ["p-exec"], declarations
    assert "p-root" not in repr(declarations)
