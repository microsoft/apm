"""Mutation guards for canonical installed Agent Plugin lifecycle state."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "message"),
    [
        (
            "src/apm_cli/deps/lockfile.py",
            "class InstalledPluginRecord:",
            "class ParallelInstalledPluginRecord:",
            "record and codec must have one lockfile owner",
        ),
        (
            "src/apm_cli/install/agent_plugin_runtime.py",
            '"""Compatibility facade for canonical installed Agent Plugin roots."""',
            '"""Compatibility facade for canonical installed Agent Plugin roots."""\n'
            "class InstalledPluginRecord:\n"
            "    pass\n",
            "record and codec must have one lockfile owner",
        ),
        (
            "src/apm_cli/install/agent_plugin_runtime.py",
            "plugin = load_agent_plugin(Path(bundle_info.source_dir))",
            "return Path(bundle_info.source_dir).name",
            "runtime storage identity must come from canonical AgentPlugin IR",
        ),
        (
            "src/apm_cli/core/deployment_ledger.py",
            "records.pop(key, None)",
            "records[key] = prior",
            "ledger ownership must replace, never union",
        ),
        (
            "src/apm_cli/core/deployment_ledger.py",
            "active_prior.active_owner != owner",
            "False",
            "ledger ownership must reject unrelated takeover",
        ),
        (
            "src/apm_cli/install/agent_plugin_state.py",
            "logical_plugin, logical_data = InstalledPluginRecordCodec.root_values(",
            "logical_plugin, logical_data = bypass_root_values(",
            "PLUGIN_ROOT and PLUGIN_DATA must route through the lockfile codec",
        ),
        (
            "src/apm_cli/deps/lockfile.py",
            'InstalledPluginRecordCodec.validate_rows(data["installed_plugins"])',
            "pass",
            "malformed installed plugin lock state must fail closed",
        ),
        (
            "src/apm_cli/deps/lockfile.py",
            'return "3"',
            'return "2"',
            "lifecycle state must require lockfile version 3",
        ),
        (
            "src/apm_cli/install/agent_plugin_state.py",
            "plugin = validation.agent_plugin",
            "plugin = None",
            "installed state projection must retain canonical ValidationResult IR",
        ),
        (
            "src/apm_cli/install/agent_plugin_state.py",
            "current = resolved_candidate",
            "current = lexical_candidate",
            "managed plugin paths must walk canonical contained ancestors",
        ),
    ],
)
def test_agent_plugin_state_guard_breaks_on_authority_mutation(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
    message: str,
) -> None:
    """Each lifecycle authority mutation must make the static guard fail."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    paths = (
        "src/apm_cli/deps/lockfile.py",
        "src/apm_cli/install/agent_plugin_runtime.py",
        "src/apm_cli/install/agent_plugin_state.py",
        "src/apm_cli/core/deployment_ledger.py",
        "scripts/check_agent_plugin_state_authority.py",
    )
    for relative in paths:
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)
    mutation = sandbox / relative_path
    source = mutation.read_text(encoding="utf-8")
    assert old in source
    mutation.write_text(source.replace(old, new, 1), encoding="utf-8")

    result = subprocess.run(
        (
            sys.executable,
            str(sandbox / "scripts/check_agent_plugin_state_authority.py"),
            str(sandbox),
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert message in result.stdout
