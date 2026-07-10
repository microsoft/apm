"""Integration coverage for marketplace plugin bin/ trust posture handling."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.exec_gate import log_bin_status
from apm_cli.install.services import _resolve_bin_skip
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import PackageInfo, PackageType, validate_apm_package

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX mode hardening only")


class _RecordingLogger:
    """Capture ``progress()`` messages for trust-posture assertions."""

    def __init__(self) -> None:
        self.progress_messages: list[str] = []

    def progress(self, message: str, **_kwargs: Any) -> None:
        """Record a progress line."""
        self.progress_messages.append(message)


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


def test_trust_bin_true_deploys_without_warning(tmp_path: Path) -> None:
    """Explicit trust should deploy bin/ without the consent warning."""
    project_root = tmp_path / "home"
    project_root.mkdir()
    (project_root / ".claude").mkdir()
    package_info, _source_bin = _write_marketplace_plugin(
        tmp_path / "packages" / "trusted-tool",
        manifest_name="MyOwner/TrustedTool",
    )
    logger = _RecordingLogger()

    result = SkillIntegrator().integrate_package_skill(
        package_info,
        project_root,
        scope=InstallScope.USER,
        logger=logger,
        trust_bin=True,
    )

    deployed_bin = project_root / ".claude" / "skills" / "trusted-tool" / "bin" / "tool"
    assert result.bin_deployed > 0
    assert deployed_bin.is_file()
    assert not any(
        "invoked without confirmation" in message for message in logger.progress_messages
    )


def test_trust_bin_false_skips_with_not_trusted_reason(tmp_path: Path) -> None:
    """Explicit no-trust should skip bin/ deployment with the trust-specific reason."""
    project_root = tmp_path / "home"
    project_root.mkdir()
    (project_root / ".claude").mkdir()
    package_info, _source_bin = _write_marketplace_plugin(
        tmp_path / "packages" / "distrusted-tool",
        manifest_name="MyOwner/DistrustedTool",
    )

    result = SkillIntegrator().integrate_package_skill(
        package_info,
        project_root,
        scope=InstallScope.USER,
        skip_bin=True,
        bin_skip_reason_override="not_trusted",
    )

    deployed_bin = project_root / ".claude" / "skills" / "distrusted-tool" / "bin" / "tool"
    assert result.bin_deployed == 0
    assert result.bin_skipped_reason == "not_trusted"
    assert not deployed_bin.exists()


def test_trust_bin_none_deploys_with_warning(tmp_path: Path) -> None:
    """Default trust posture should deploy bin/ and emit the acknowledgement warning."""
    project_root = tmp_path / "home"
    project_root.mkdir()
    (project_root / ".claude").mkdir()
    package_info, _source_bin = _write_marketplace_plugin(
        tmp_path / "packages" / "prompted-tool",
        manifest_name="MyOwner/PromptedTool",
    )
    logger = _RecordingLogger()

    result = SkillIntegrator().integrate_package_skill(
        package_info,
        project_root,
        scope=InstallScope.USER,
        logger=logger,
        trust_bin=None,
    )

    deployed_bin = project_root / ".claude" / "skills" / "prompted-tool" / "bin" / "tool"
    assert result.bin_deployed > 0
    assert deployed_bin.is_file()
    assert any("invoked without confirmation" in message for message in logger.progress_messages)


def test_log_bin_status_not_trusted() -> None:
    """Status logging should point users at the trust flags when bin/ is skipped."""
    skill_result = SimpleNamespace(bin_deployed=0, bin_skipped_reason="not_trusted")
    lines: list[str] = []

    log_bin_status(skill_result, "", "pkg", SimpleNamespace(name="pkg"), lines.append)

    assert lines == ["  |-- bin/ executables skipped (--no-trust-bin). Pass --trust-bin to deploy."]


@pytest.mark.parametrize(
    ("bin_approved", "trust_bin", "expected"),
    [
        (True, None, (False, None)),
        (True, True, (False, None)),
        (True, False, (True, "not_trusted")),
        (False, None, (True, "not_approved")),
        (False, True, (True, "not_approved")),
        (False, False, (True, "not_approved")),
    ],
)
def test_resolve_bin_skip(
    bin_approved: bool,
    trust_bin: bool | None,
    expected: tuple[bool, str | None],
) -> None:
    """allowExecutables approval should override trust posture when bin/ is blocked."""
    assert _resolve_bin_skip(bin_approved, trust_bin) == expected
