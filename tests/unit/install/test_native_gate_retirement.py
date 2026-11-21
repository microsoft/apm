"""Gate retirement judges a locked native plugin as install's admission does (#3921)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.admission import locked_native_retired
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins.record import NativePluginRecord

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I4")]

_KEY = "acme/tool"


def _lock(modules: Path) -> LockFile:
    root = modules / "acme/tool"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "tool"}))
    (root / "bin").mkdir()
    (root / "bin/tool").write_text("#!/bin/sh\n")
    dependency = LockedDependency(
        repo_url=_KEY, resolved_commit="a" * 40, package_type="marketplace_plugin"
    )
    dependency.native_plugin = NativePluginRecord(
        "tool", "sha256:" + "0" * 64, "apm-p-00000000", "./.agents/plugins"
    )
    return LockFile(dependencies={_KEY: dependency})


def _policy(**bin_deploy) -> SimpleNamespace:
    rules = SimpleNamespace(**{"deny_all": False, "deny": (), **bin_deploy})
    return SimpleNamespace(bin_deploy=rules, mcp=None, enforcement="block")


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (_policy(), {}),
        (
            _policy(deny_all=True),
            {_KEY: "org policy bin_deploy.deny_all blocks its bin/ executables"},
        ),
        (
            _policy(deny=(_KEY,)),
            {_KEY: "org policy bin_deploy.deny blocks its bin/ executables"},
        ),
    ],
    ids=["allowed", "deny-all", "deny-package"],
)
def test_an_org_bin_deploy_deny_retires_a_native_plugin_that_ships_bin(
    tmp_path: Path, policy: SimpleNamespace, expected: dict[str, str]
) -> None:
    """With the gate off, only the org bin_deploy rule decides, as install's park does."""
    modules = tmp_path / "apm_modules"
    retired = locked_native_retired(_lock(modules), modules, lambda: None, policy)
    assert retired == expected
