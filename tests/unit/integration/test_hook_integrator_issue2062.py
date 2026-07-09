"""Regression coverage for Claude flat hook normalization (#2062)."""

import json
from pathlib import Path

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo


def test_claude_wraps_flat_hook_entries_in_settings_and_sidecar(tmp_path: Path) -> None:
    """Claude receives matcher groups while ownership tracks the same shape."""
    package_root = tmp_path / "package"
    hooks_dir = package_root / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "test.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"type": "command", "command": "echo hi", "timeout": 15}]}}
        ),
        encoding="utf-8",
    )
    package = PackageInfo(
        package=APMPackage(name="hook-repro-pkg", version="1.0.0"),
        install_path=package_root,
    )
    project_root = tmp_path / "consumer"
    (project_root / ".claude").mkdir(parents=True)

    result = HookIntegrator().integrate_package_hooks_claude(package, project_root)

    expected_entry = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "echo hi", "timeout": 15}],
    }
    settings = json.loads((project_root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    sidecar = json.loads((project_root / ".claude" / "apm-hooks.json").read_text(encoding="utf-8"))
    assert result.files_integrated == 1
    assert settings["hooks"]["PreToolUse"] == [expected_entry]
    assert sidecar["PreToolUse"] == [{**expected_entry, "_apm_source": "package"}]
