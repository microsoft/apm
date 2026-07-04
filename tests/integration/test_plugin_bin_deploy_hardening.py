"""Integration coverage for marketplace plugin bin/ deployment hardening."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import PackageInfo, PackageType, validate_apm_package
from apm_cli.policy.schema import ApmPolicy, BinDeployPolicy

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX mode hardening only")


def _write_marketplace_plugin(package_dir: Path, *, manifest_name: str) -> tuple[PackageInfo, Path]:
    """Create and validate a real marketplace plugin fixture with one bin file."""
    package_dir.mkdir(parents=True)
    (package_dir / "plugin.json").write_text(
        f'{{"name": "{manifest_name}", "version": "1.0.0"}}',
        encoding="utf-8",
    )
    plugin_manifest_dir = package_dir / ".claude-plugin"
    plugin_manifest_dir.mkdir()
    (plugin_manifest_dir / "plugin.json").write_text(
        f'{{"name": "{manifest_name}"}}',
        encoding="utf-8",
    )
    bin_dir = package_dir / "bin"
    bin_dir.mkdir()
    source_bin = bin_dir / "tool"
    source_bin.write_text("#!/bin/sh\necho plugin\n", encoding="utf-8")
    source_bin.chmod(0o777)

    validation = validate_apm_package(package_dir)
    assert not validation.errors
    assert validation.package is not None
    assert validation.package_type is PackageType.MARKETPLACE_PLUGIN
    return (
        PackageInfo(
            package=validation.package,
            install_path=package_dir,
            package_type=validation.package_type,
        ),
        source_bin,
    )


def test_plugin_bin_deploy_hardens_mode_and_honors_normalized_legacy_deny(
    tmp_path: Path,
) -> None:
    """Real plugin deploy path chmods bins to 0o700 and normalizes legacy denies."""
    project_root = tmp_path / "home"
    project_root.mkdir()
    (project_root / ".claude").mkdir()

    integrator = SkillIntegrator()
    allowed_info, allowed_source_bin = _write_marketplace_plugin(
        tmp_path / "packages" / "loose-tool",
        manifest_name="MyOwner/LooseTool",
    )

    assert stat.S_IMODE(allowed_source_bin.stat().st_mode) == 0o777
    allowed = integrator.integrate_package_skill(
        allowed_info,
        project_root,
        scope=InstallScope.USER,
    )

    deployed_bin = project_root / ".claude" / "skills" / "loose-tool" / "bin" / "tool"
    assert allowed.bin_deployed == 2
    assert deployed_bin.is_file()
    assert stat.S_IMODE(deployed_bin.stat().st_mode) == 0o700

    denied_info, denied_source_bin = _write_marketplace_plugin(
        tmp_path / "packages" / "denied-tool",
        manifest_name="MyOwner/MyPlugin",
    )
    policy = ApmPolicy(
        bin_deploy=BinDeployPolicy(deny=("https://github.com/myowner/myplugin.git",))
    )

    assert stat.S_IMODE(denied_source_bin.stat().st_mode) == 0o777
    denied = integrator.integrate_package_skill(
        denied_info,
        project_root,
        scope=InstallScope.USER,
        policy=policy,
    )

    denied_bin = project_root / ".claude" / "skills" / "denied-tool" / "bin" / "tool"
    assert denied.bin_deployed == 0
    assert not denied_bin.exists()
